python train.py --task language_modeling data-bin/enwik8 \
    --save-dir output/enwik8-38M-trime_long \
    --arch transformer_lm_enwik8 \
    --criterion trime_long_loss_same_device \
    --optimizer adam --adam-betas "(0.9, 0.98)" --weight-decay 0.01 \
    --clip-norm 0.0 --max-update 400000 --max-lr 0.00025 --lr 0.0 \
    --lr-scheduler cosine --warmup-updates 0 \
    --max-tokens 12288 --update-freq 1 --tokens-per-sample 512 \
    --seed 1 --sample-break-mode none --skip-invalid-size-inputs-valid-test \
    --ddp-backend=no_c10d --adaptive-input --tie-adaptive-weights --adaptive-input-cutoff 208 --adaptive-softmax-cutoff 208 \
    --knn-keytype last_ffn_input --adaptive-softmax-factor 1 --adaptive-input-factor 1 --fp16 \
    --ce-warmup-epoch 3 --keep-order
