#!/usr/bin/env python3 -u
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""
Evaluate the perplexity of a trained language model.
"""

import logging
import math
import os
import json

import torch
import numpy as np

from fairseq import checkpoint_utils, options, progress_bar, tasks, utils
from fairseq.data import LMContextWindowDataset
from fairseq.meters import StopwatchMeter, TimeMeter
from fairseq.sequence_scorer import SequenceScorer
from fairseq.knnlm import KNN_Dstore


logging.basicConfig(
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    level=logging.INFO,
)
logger = logging.getLogger('fairseq_cli.eval_lm')


class WordStat(object):
    def __init__(self, word, is_bpe):
        self.word = word
        self.is_bpe = is_bpe
        self.log_prob = 0
        self.next_word_prob = 0
        self.count = 0
        self.missing_next_words = 0

    def add(self, log_prob, next_word_prob):
        """ increments counters for the sum of log probs of current word and next
            word (given context ending at current word). Since the next word might be at the end of the example,
            or it might be not counted because it is not an ending subword unit,
            also keeps track of how many of those we have seen """
        if next_word_prob is not None:
            self.next_word_prob += next_word_prob
        else:
            self.missing_next_words += 1
        self.log_prob += log_prob
        self.count += 1

    def __str__(self):
        return '{}\t{}\t{}\t{}\t{}\t{}'.format(self.word, self.count, self.log_prob, self.is_bpe,
                                               self.next_word_prob, self.count - self.missing_next_words)


def main(parsed_args):
    assert parsed_args.path is not None, '--path required for evaluation!'

    utils.import_user_module(parsed_args)

    logger.info(parsed_args)

    use_cuda = torch.cuda.is_available() and not parsed_args.cpu

    task = tasks.setup_task(parsed_args)

    # Load ensemble
    logger.info('loading model(s) from {}'.format(parsed_args.path))
    models, args = checkpoint_utils.load_model_ensemble(
        parsed_args.path.split(os.pathsep),
        arg_overrides=eval(parsed_args.model_overrides),
        task=task,
    )

    for arg in vars(parsed_args).keys():
        if arg not in {
            'self_target', 'future_target', 'past_target', 'tokens_per_sample',
            'output_size_dictionary', 'add_bos_token',
        }:
            setattr(args, arg, getattr(parsed_args, arg))
    args.keep_order = False

    # reduce tokens per sample by the required context window size
    print('Tokens per sample:', args.tokens_per_sample)
    print('Context window:', args.context_window)
    args.tokens_per_sample -= args.context_window
    task = tasks.setup_task(args)

    # Load dataset splits
    task.load_dataset(args.gen_subset)
    dataset = task.dataset(args.gen_subset)
    if args.context_window > 0:
        print('Building LMContextWindowDataset w/ tokens_per_sample = %d'%args.tokens_per_sample)
        dataset = LMContextWindowDataset(
            dataset=dataset,
            tokens_per_sample=args.tokens_per_sample,
            context_window=args.context_window,
            pad_idx=task.source_dictionary.pad(),
        )
    max_length = 0
    min_length = 512000
    tot = 0
    for sample in dataset:
        max_length = max(sample['source'].shape[0], max_length)
        min_length = min(sample['source'].shape[0], min_length)
        tot += sample['source'].shape[0]
    logger.info('{} {} {} examples'.format(args.data, args.gen_subset, len(dataset)))
    logger.info('Per example, max: {}, min: {}, tot: {}'.format(max_length, min_length, tot))

    # Optimize ensemble for generation and set the source and dest dicts on the model (required by scorer)
    for model in models:
        model.make_generation_fast_()
        if args.fp16:
            model.half()
        if use_cuda:
            model.cuda()

    assert len(models) > 0

    logger.info('num. model params: {}'.format(sum(p.numel() for p in models[0].parameters())))

    logger.info('Args: max_tokens = {}, max_sentences = {}, num_shards = {}, shard_id = {}'.format(args.max_tokens, args.max_sentences, args.num_shards, args.shard_id))
    itr = task.get_batch_iterator(
        dataset=dataset,
        max_tokens=args.max_tokens or 36000,
        max_sentences=args.max_sentences,
        max_positions=utils.resolve_max_positions(*[
            model.max_positions() for model in models
        ]),
        ignore_invalid_inputs=True,
        num_shards=args.num_shards,
        shard_id=args.shard_id,
        num_workers=args.num_workers,
    ).next_epoch_itr(shuffle=False)

    gen_timer = StopwatchMeter()
    scorer = SequenceScorer(task.target_dictionary, args.softmax_batch, args=args)

    score_sum = 0.
    count = 0

    if args.remove_bpe is not None:
        if args.remove_bpe == 'sentencepiece':
            raise NotImplementedError
        else:
            bpe_cont = args.remove_bpe.rstrip()
            bpe_toks = {
                i
                for i in range(len(task.source_dictionary))
                if task.source_dictionary[i].endswith(bpe_cont)
            }
        bpe_len = len(bpe_cont)
    else:
        bpe_toks = None
        bpe_len = 0

    word_stats = dict()

    if args.knnlm and args.save_knnlm_dstore:
        raise ValueError("Cannot use knnlm while trying to build the datastore!")

    if args.knnlm:
        knn_dstore = KNN_Dstore(args)
        logger.info('Finished reading KNN Dstore')

    with progress_bar.build_progress_bar(args, itr) as t:
        wps_meter = TimeMeter()

        if args.save_knnlm_dstore:
            print('keytype being saved:', args.knn_keytype)
            print('dim:', args.decoder_embed_dim)
            if args.dstore_fp16:
                print('Saving fp16')
                dstore_prob = np.memmap(args.dstore_mmap+'_prob.npy', dtype=np.float16, mode='w+', shape=(args.dstore_size, 1))
                dstore_keys = np.memmap(args.dstore_mmap+'-fp16_keys.npy', dtype=np.float16, mode='w+', shape=(args.dstore_size, args.decoder_embed_dim))
                dstore_vals = np.memmap(args.dstore_mmap+'_vals.npy', dtype=np.int32, mode='w+', shape=(args.dstore_size, 1))
            else:
                print('Saving fp32')
                dstore_prob = np.memmap(args.dstore_mmap+'_prob.npy', dtype=np.float32, mode='w+', shape=(args.dstore_size, 1))
                dstore_keys = np.memmap(args.dstore_mmap+'_keys.npy', dtype=np.float32, mode='w+', shape=(args.dstore_size, args.decoder_embed_dim))
                dstore_vals = np.memmap(args.dstore_mmap+'_vals.npy', dtype=np.int32, mode='w+', shape=(args.dstore_size, 1))
            print(f"prob = {dstore_prob.shape} {dstore_prob.dtype}")
            print(f"keys = {dstore_keys.shape} {dstore_keys.dtype}")
            print(f"vals = {dstore_vals.shape} {dstore_vals.dtype}")

        dstore_idx = 0
        for ex_i, sample in enumerate(t):

            if 'net_input' not in sample:
                continue

            sample = utils.move_to_cuda(sample) if use_cuda else sample

            gen_timer.start()
            if args.knnlm:
                hypos = scorer.generate(models, sample, knn_dstore=knn_dstore)
            else:
                hypos = scorer.generate(models, sample)
            gen_timer.stop(sample['ntokens'])

            done = False
            for i, hypos_i in enumerate(hypos):
                hypo = hypos_i[0]
                if args.save_knnlm_dstore:
                    shape = hypo['dstore_keys'].shape
                    if dstore_idx >= args.dstore_size:
                        done = True
                        print('Already done. Skipping.')
                    elif shape[0] == args.tokens_per_sample or args.no_min_context:
                        #assert not done
                        if True:
                            for k in ['dstore_keys', 'tokens', 'positional_scores']:
                                assert hypo[k].shape[0] >= shape[0], (k, hypo[k].shape, shape)

                        if dstore_idx + shape[0] > args.dstore_size:
                            shape = [args.dstore_size - dstore_idx]
                            for k in ['dstore_keys', 'tokens', 'positional_scores']:
                                hypo[k] = hypo[k][:shape[0]]
                        if args.dstore_fp16:
                            dstore_prob[dstore_idx:shape[0]+dstore_idx] = hypo['positional_scores'].view(-1, 1).cpu().numpy().astype(np.float16)
                            dstore_keys[dstore_idx:shape[0]+dstore_idx] = hypo['dstore_keys'].view(
                                -1, args.decoder_embed_dim).cpu().numpy().astype(np.float16)
                            #dstore_vals[dstore_idx:shape[0]+dstore_idx] = hypo['tokens'].view(
                            #    -1, 1).cpu().numpy().astype(np.int)
                            # Double check for overflow, which can happen easily with int16.
                            tmp = hypo['tokens'].view(-1, 1).cpu().numpy().astype(np.int32)
                            #assert (tmp >= 0).all().item()
                            dstore_vals[dstore_idx:shape[0]+dstore_idx] = tmp
                        else:
                            dstore_prob[dstore_idx:shape[0]+dstore_idx] = hypo['positional_scores'].view(-1, 1).cpu().numpy().astype(np.float32)
                            dstore_keys[dstore_idx:shape[0]+dstore_idx] = hypo['dstore_keys'].view(
                                -1, args.decoder_embed_dim).cpu().numpy().astype(np.float32)
                            dstore_vals[dstore_idx:shape[0]+dstore_idx] = hypo['tokens'].view(
                                -1, 1).cpu().numpy().astype(np.int)

                        dstore_idx += shape[0]
                    else:
                        done = True
                        print('Skipping this one with shape', shape)

                sample_id = sample['id'][i]

                tokens = hypo['tokens']
                tgt_len = tokens.numel()
                pos_scores = hypo['positional_scores'].float()

                if args.add_bos_token:
                    assert hypo['tokens'][0].item() == task.target_dictionary.bos()
                    tokens = tokens[1:]
                    pos_scores = pos_scores[1:]

                skipped_toks = 0
                if bpe_toks is not None:
                    for i in range(tgt_len - 1):
                        if tokens[i].item() in bpe_toks:
                            skipped_toks += 1
                            pos_scores[i + 1] += pos_scores[i]
                            pos_scores[i] = 0

                #inf_scores = pos_scores.eq(float('inf')) | pos_scores.eq(float('-inf'))
                #if inf_scores.any():
                #    logger.info(
                #        'skipping tokens with inf scores:',
                #        task.target_dictionary.string(tokens[inf_scores.nonzero()])
                #    )
                #    pos_scores = pos_scores[(~inf_scores).nonzero()]
                score_sum += pos_scores.sum().cpu()
                count += pos_scores.numel() - skipped_toks

                if args.output_word_probs or args.output_word_stats:
                    w = ''
                    word_prob = []
                    is_bpe = False
                    for i in range(len(tokens)):
                        w_ind = tokens[i].item()
                        w += task.source_dictionary[w_ind]
                        if bpe_toks is not None and w_ind in bpe_toks:
                            w = w[:-bpe_len]
                            is_bpe = True
                        else:
                            word_prob.append((w, pos_scores[i].item()))

                            next_prob = None
                            ind = i + 1
                            while ind < len(tokens):
                                if pos_scores[ind].item() != 0:
                                    next_prob = pos_scores[ind]
                                    break
                                ind += 1

                            word_stats.setdefault(w, WordStat(w, is_bpe)).add(pos_scores[i].item(), next_prob)
                            is_bpe = False
                            w = ''
                    if args.output_word_probs:
                        logger.info(
                            str(int(sample_id)) + " "
                            + ('\t'.join('{} [{:2f}]'.format(x[0], x[1]) for x in word_prob))
                        )

            wps_meter.update(sample['ntokens'])
            t.log({'wps': round(wps_meter.avg)})

    if args.save_knnlm_dstore:
        print("dstore_idx", dstore_idx, "final shape", shape)
        print("Keys", dstore_keys.shape, dstore_keys.dtype)
        print("Vals", dstore_vals.shape, dstore_vals.dtype)

    avg_nll_loss = -score_sum / count / math.log(2)  # convert to base 2
    logger.info('Evaluated {} tokens in {:.1f}s ({:.2f} tokens/s)'.format(
        gen_timer.n, gen_timer.sum, 1. / gen_timer.avg
    ))
    logger.info('Loss (base 2): {:.4f}, Perplexity: {:.2f}'.format(
        avg_nll_loss, 2**avg_nll_loss
    ))
    print('Evaluated {} tokens in {:.1f}s ({:.2f} tokens/s)'.format(
        gen_timer.n, gen_timer.sum, 1. / gen_timer.avg
    ))
    print('Loss (base 2): {:.4f}, Perplexity: {:.2f}'.format(
        avg_nll_loss, 2**avg_nll_loss
    ))

    print(avg_nll_loss, 2**avg_nll_loss)

    if args.eval_file is not None:
        eval_res = {'loss': float(avg_nll_loss), 'perplexity': float(2**avg_nll_loss), 'lmbda': args.lmbda, 'temp': args.softmax_temp}
        with open(args.eval_file, 'w') as f:
            json.dump(eval_res, f)

    if args.output_word_stats:
        for ws in sorted(word_stats.values(), key=lambda x: x.count, reverse=True):
            logger.info(ws)


def cli_main():
    parser = options.get_eval_lm_parser()
    args = options.parse_args_and_arch(parser)
    main(args)


if __name__ == '__main__':
    cli_main()
