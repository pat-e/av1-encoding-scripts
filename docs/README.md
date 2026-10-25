# Docs

| File | What it is |
| :--- | :--- |
| [GUIDE.md](GUIDE.md) | How the encoders run, and why (HandBrake vs remux, audio, subtitle fonts, folders) |
| [parameters.md](parameters.md) | Encoder flags, loudnorm, CFR probe, av1an/xav command lines |

Crop detection lives in [`../cropdetect/`](../cropdetect/). Denoise and film grain live in [`../tools/`](../tools/). `mdegrain.py` defaults to thsad 350. `--low` is 150, `--high` is 500, and `--thsad N` sets the strength.
