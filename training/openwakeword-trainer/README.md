# openwakeword-trainer

Train custom wake word models with [openWakeWord](https://github.com/dscripka/openWakeWord). A granular 13-step pipeline with compatibility patches for torchaudio 2.10+, Piper TTS, and speechbrain. Generates tiny ONNX models (~200 KB) for real-time keyword detection — like building your own "Hey Siri" trigger.

## What It Does

This toolkit automates the entire openWakeWord training process:

1. **Synthesizes** thousands of speech clips using Piper TTS with varied voices and accents
2. **Augments** clips with real-world noise, music, and room impulse responses
3. **Trains** a small DNN classifier optimized for always-on, low-latency detection
4. **Exports** a tiny ONNX model you can deploy anywhere

The result is a ~200 KB model that runs on CPU in real-time with negligible resource usage.

## Prerequisites

| Requirement | Details |
|-------------|---------|
| **WSL2 or Linux** | Ubuntu recommended (`wsl --install -d Ubuntu` on Windows) |
| **NVIDIA GPU** | CUDA drivers installed (WSL2 includes CUDA passthrough automatically) |
| **Disk space** | ~15 GB free (temporary downloads; deletable after training) |
| **Python 3.10+** | Inside WSL2/Linux (`python3 --version`) |
| **Time** | ~1–2 hours with GPU, 12–24 hours CPU-only |

### Verify CUDA (WSL2)

```bash
wsl
nvidia-smi
```

You should see your GPU listed. If not, update your NVIDIA Windows driver to the latest version.

## Quick Start

### Option A: One-liner

```bash
# From PowerShell (Windows) — cd to the repo first:
cd path\to\openwakeword-trainer
wsl -- bash train.sh

# Or from within WSL2/Linux:
cd /mnt/c/path/to/openwakeword-trainer
bash train.sh
```

This creates an isolated virtualenv, installs dependencies, downloads datasets, trains the model, and exports the result.

### Option B: Step-by-step

```bash
# Enter WSL2 and navigate to the repo
wsl
cd /mnt/c/path/to/openwakeword-trainer

# Create & activate a training venv (use native filesystem, not /mnt/c/)
python3 -m venv ~/.oww-trainer-venv
source ~/.oww-trainer-venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the full pipeline
python train_wakeword.py

# Or resume from where you left off
python train_wakeword.py --from augment
```

### Train Your Own Wake Word

1. Copy the example config:
   ```bash
   cp configs/hey_echo.yaml configs/my_word.yaml
   ```

2. Edit `configs/my_word.yaml`:
   ```yaml
   model_name: "my_word"
   target_phrase:
     - "hey computer"
   custom_negative_phrases:
     - "hey commuter"
     - "computer"
     - "hey"
   ```

3. Train:
   ```bash
   python train_wakeword.py --config configs/my_word.yaml
   ```

4. Find your model in `export/my_word.onnx` (and `export/my_word.onnx.data`).

### Improve with Real Voice Recordings

Synthetic TTS clips get you a working model, but real voice recordings significantly improve accuracy. Use the recording script in the parent `training/` directory:

```bash
cd ../

# Positive clips (the wake word itself)
python3 record_wake_word.py --phrase "hey computer" --count 50
python3 record_wake_word.py --phrase "hey computer" --count 50 --auto  # auto mode

# See suggested negative phrases to reduce false positives
python3 record_wake_word.py --suggest

# Negative clips (similar-sounding phrases the model should reject)
python3 record_wake_word.py --phrase "hey commuter" --count 10 --negative --auto
```

Tips for better results:
- Record 200+ positive clips from multiple speakers
- Record 50-100 negative clips from similar-sounding phrases (use `--suggest` for ideas)
- Vary distance, volume, and speaking speed between clips
- Set `augmentation_rounds: 3` in your config for more data diversity
- Positive clips go in `real_clips/<phrase>/`, negative clips go in `real_clips_negative/<phrase>/`

After recording, retrain:
```bash
cd openwakeword-trainer/
python train_wakeword.py --config configs/my_word.yaml
```

## Pipeline Steps

The pipeline runs **13 granular steps**, each with built-in verification. If any step fails, it stops immediately and tells you exactly how to resume.

| # | Step | Description | Time |
|---|------|-------------|------|
| 1 | `check-env` | Verify Python, CUDA, critical imports | instant |
| 2 | `apply-patches` | Patch torchaudio/speechbrain/piper compat | instant |
| 3 | `download` | Download datasets, Piper TTS model, tools | ~30 min |
| 4 | `verify-data` | Check all data files present & sizes | instant |
| 5 | `resolve-config` | Resolve config paths to absolute | instant |
| 6 | `generate` | Generate clips via Piper TTS | ~10 min (GPU) |
| 7 | `resample-clips` | Spot-check clip sample rates | instant |
| 8 | `verify-clips` | Verify clip counts and directories | instant |
| 9 | `augment` | Augment clips & extract mel features | ~30 min |
| 10 | `verify-features` | Check `.npy` feature files & shapes | instant |
| 11 | `train` | Train DNN model + ONNX export | ~30 min (GPU) |
| 12 | `verify-model` | Load-test with ONNX Runtime | instant |
| 13 | `export` | Copy model to `export/` directory | instant |

If any step fails:
```
Pipeline stopped.  Fix the issue above, then resume:
  python train_wakeword.py --from <failed-step>
```

## CLI Reference

```bash
# Full pipeline (all 13 steps)
python train_wakeword.py

# Use a custom config
python train_wakeword.py --config configs/my_word.yaml

# Resume from a specific step
python train_wakeword.py --from augment

# Run exactly one step
python train_wakeword.py --step verify-clips

# Check current state without side effects
python train_wakeword.py --verify-only

# Show all available steps
python train_wakeword.py --list-steps
```

## Using Your Model

The export step produces two files that must be kept together:

- `hey_echo.onnx` — the model graph (~14 KB)
- `hey_echo.onnx.data` — external weights (~200 KB)

Copy **both** files to your project. The trained model works with any openWakeWord-compatible runtime:

```python
from openwakeword.model import Model

oww = Model(wakeword_models=["export/hey_echo.onnx"])

# Feed 16 kHz audio frames
prediction = oww.predict(audio_frame)
```

Or with ONNX Runtime directly:

```python
import onnxruntime as ort
import numpy as np

sess = ort.InferenceSession("export/hey_echo.onnx")
# Input shape: [1, 16, 96] (mel spectrogram features)
result = sess.run(None, {"x": features})
```

## Configuration Reference

See [configs/hey_echo.yaml](configs/hey_echo.yaml) for a fully commented example. Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `model_name` | — | Name for the model (used for filenames) |
| `target_phrase` | — | List of phrases to detect |
| `custom_negative_phrases` | `[]` | Phrases to explicitly reject |
| `n_samples` | `50000` | Number of positive training clips |
| `tts_batch_size` | `25` | Piper TTS batch size (reduce for low VRAM) |
| `model_type` | `"dnn"` | `"dnn"` or `"rnn"` |
| `layer_size` | `32` | Hidden layer size (32=fast, 64/128=higher capacity) |
| `steps` | `50000` | Training steps |
| `target_false_positives_per_hour` | `0.2` | Target false positive rate |

## Threshold Tuning

After training, tune the detection threshold for your use case:

| Problem | Fix |
|---------|-----|
| False activations (triggers when you didn't say it) | Increase threshold: 0.5 → 0.6 → 0.7 |
| Missed activations (need to over-pronounce) | Decrease threshold: 0.5 → 0.4 → 0.3 |
| False triggers on similar words | Add to `custom_negative_phrases` and retrain |

## Compatibility Patches

This toolkit includes automatic patches for known breaking changes in modern dependency versions:

| Issue | Affected | Patch |
|-------|----------|-------|
| `torchaudio.load()` removed | torchaudio ≥2.10 | Soundfile-based replacement with automatic 22050→16000 Hz resampling |
| `torchaudio.info()` removed | torchaudio ≥2.10 | Soundfile-based metadata reader |
| `torchaudio.list_audio_backends()` removed | torchaudio ≥2.10 | Returns `["soundfile"]` for speechbrain compat |
| `pkg_resources` removed | setuptools ≥82 | Auto-installs setuptools<82 |
| Piper API change | piper-sample-generator v2+ | Auto-resolves `model=` kwarg |
| `torchcodec` missing | MIT RIR dataset loading | `pip install torchcodec` |
| `sph_harm` renamed to `sph_harm_y` | scipy ≥1.17 + acoustics lib | Wrapper with swapped args |

Patches are applied and verified automatically during the `apply-patches` step.

## Cleanup

After training, reclaim disk space:

```bash
rm -rf data/          # ~12 GB of downloaded datasets
rm -rf output/        # intermediate training artifacts
```

Keep only the `export/` directory with your trained model.

## Troubleshooting

### `piper-phonemize` fails to install
This package only has Linux wheels. Make sure you're running inside WSL2, not native Windows.

### `nvidia-smi` not found in WSL2
Update your NVIDIA Windows driver to the latest version. WSL2 CUDA passthrough is included automatically.

### Training is very slow
Verify CUDA is available: `python -c "import torch; print(torch.cuda.is_available())"`. If `False`, everything falls back to CPU.

### Out of GPU memory
Reduce `tts_batch_size` in your config (e.g., 25 → 10).

### Download stalls
Re-run the script — all downloads are idempotent and resume where they left off.

### `ImportError: torchcodec`
Install it: `pip install torchcodec`. Required for loading MIT RIR audio datasets.

### `ImportError: sph_harm` (acoustics library)
scipy 1.17+ renamed `sph_harm` to `sph_harm_y` with different argument order. The pipeline patches this automatically, but if you see this error, check `docs/training_notes.md` for the manual fix.

### Model detects well in training but poorly on real speech
Train with real voice recordings — see "Improve with Real Voice Recordings" above. Synthetic-only models score inconsistently on actual speech. Lower the detection threshold (0.5 or lower) for custom models.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgments

- [openWakeWord](https://github.com/dscripka/openWakeWord) by David Scripka
- [Piper](https://github.com/rhasspy/piper) by Rhasspy for synthetic TTS
- Built with PyTorch, ONNX Runtime, and speechbrain
