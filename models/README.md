# RNNoise model

`processing_scripts/preprocessing_worker.py` expects an FFmpeg-compatible RNNoise model at
`models/rnnoise.rnnn` by default. The intended baseline is `cb.rnnn` from the
GregorR/rnnoise-models `conjoined-burgers-2018-08-28` directory, described by its
repository as a model trained for general recording noise and general signal.

The production manifest records the model path and SHA-256 digest for every run.
Set `RNNOISE_MODEL_PATH` to evaluate a different compatible model without changing
the application code.

Downloaded baseline SHA-256:

```text
f1357c4e5be9dee8467bead486dfced2d75b640c26ad0b594fa7f102322371d9  rnnoise.rnnn
```
