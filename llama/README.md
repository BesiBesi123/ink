# ink's llama.cpp build

ink runs its models on [llama.cpp](https://github.com/ggml-org/llama.cpp) (MIT). The release binaries of this repo are
upstream llama.cpp at a pinned commit (`LLAMA_COMMIT`) plus the patches in `patches/`, built by
`.github/workflows/build-llama.yml` for Windows x64 + CUDA 12.4.

| Patch | Why |
|---|---|
| `0001-ggml-backend-split-local-host-weight-copies-32910.diff` | Looped MoE models (Inkling 2 Mini) asked for a ~7 GB CUDA compute buffer whenever host-resident expert weights were offloaded for prompt processing, so on 8 GB cards they had to run with `--no-op-offload` (CPU prompt processing, ~45-70 tok/s). The patch gives each split its own copy of an offloaded host weight, so the buffer drops to one layer's worth (~0.4 GB) and op offload works again. Posted by erdincyasar in [llama.cpp issue #32910](https://github.com/ggml-org/llama.cpp/issues/32910). |

Already upstream at the pinned commit (no patch needed): Flash Attention with indexed bias (#33063, #33053) and DSpark
speculative decoding (`--spec-type draft-dspark`, #32218).

To rebuild with a newer llama.cpp: update `LLAMA_COMMIT`, check `git apply --check patches/*.diff`, run the workflow.
