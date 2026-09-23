# Android trace investigation

Measured on 2026-09-08.  These results deliberately distinguish Android code
provenance from Android-like behaviour: neither trace is a trace of a running
Android system.

## Results

All percentages are recomputed over the final 250,000 instructions.  `miss%`
is the D-cache statistic from the perf model with `model_dcache=True`; it is not
host time and it is not an RTL cache measurement.  `undecoded` and `vector` are
counts over the entire delivered trace.

| trace | instructions | load% | store% | br% | indirect% | depload% | miss% | undecoded | vector |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| CoreMark (existing) | - | 17.90 | 6.30 | 18.80 | 0.09 | 43.00 | 0.08 | - | - |
| Dhrystone (existing) | - | 26.30 | 8.10 | 22.40 | 2.19 | 6.20 | 0.05 | - | - |
| SQLite (existing) | - | 26.00 | 13.30 | 13.60 | 3.30 | 38.00 | 1.12 | - | - |
| Lua (existing ART proxy) | - | 20.70 | 11.90 | 12.30 | 3.08 | 26.60 | 1.46 | - | - |
| `tmp_android_lua_cpressure_250k.log` | 250,000 | 19.78 | 10.05 | 10.11 | 3.39 | 34.60 | **5.03** | **0** | **0** |
| `tmp_android_bionic_proxy.log` | 3,219,298 | 23.48 | 8.99 | 23.42 | 0.93 | 0.35 | **2.80** | **0** | **0** |

The Lua/C result is the useful behaviour proxy.  It retains the managed-runtime
indirect-branch signature and raises the modeled D-cache miss rate from 1.46%
to 5.03%.  It consists of Lua 5.4.6 interpreting
`android_memory_proxy.lua`, followed by calls to a C helper that performs eight
dependent accesses through a deterministic 2 MiB cache-line ring.  This is
engineered cache pressure, not a claim that 5.03% is Android's measured miss
rate; no real Android trace is available here to validate that number.

The Bionic result is the real-code provenance trace.  Its `memcmp`, `memcpy`,
`memmove`, and `memset` sources are the exact scalar files selected for
RISC-V by Bionic revision `f971dc6b4ae58ba05450ce096c543b2bf4709912`,
immediately before Android removed the non-vector path.  Symbol-PC coverage
over the full trace is:

| routine | instructions |
|---|---:|
| `bionic_memcmp` | 1,350,313 |
| `bionic_memcpy` | 284,751 |
| `bionic_memmove` | 1,040,272 |
| `bionic_memset` | 61,620 |
| **inside Bionic routines** | **2,736,956 (85.02%)** |

The driver, allocator/static C library, `pk`, and Spike are not Android.  The
routines were cross-compiled with GCC `-O2` for
`rv64imafdc_zifencei`; they are historical Android source, not the code emitted
by the current AOSP toolchain.  Current Bionic removed these scalar RISC-V
paths in commit
[`7d13666`](https://android.googlesource.com/platform/bionic/+/7d13666b536f20ac0a591cbac2f17ec5a01c0478),
whose commit message states that Android will require V.

Artifact hashes:

```
fd371be993c26ec50536100d23ede109dbd86625570e6e56c72244d29142da4d  tmp_android_lua_cpressure_250k.log
0f8f9203f60075178701f8b0a8bdbfcebeed17b6604c827fe780b06bc7c4a109  tmp_android_bionic_proxy.log
```

## Spike throughput

The inherited CVA6-tree Spike is a 243,346,160-byte ELF containing debug
information.  It was already compiled with `-O2`, so rebuilding the same fork
and merely stripping it reduced size to about 12 MiB but did not materially
change its roughly 29 kinst/s logging rate.

The delivered build uses upstream Spike revision
[`4ffd6ba`](https://github.com/riscv-software-src/riscv-isa-sim/commit/4ffd6ba860f4190ceac2716fa3c2cf139e85538f),
configured with `-O3 -DNDEBUG -march=native`, installed, stripped, and checked
at runtime for `--log-commits`.  Current upstream exposes commit logging without
a configure-time switch; the build script adds `--enable-commitlog` when the
checked-out revision advertises that option.

The comparison alternated implementations for three runs, using the same
`ws8` target and the same `pk`.  Wall time is used only to report host trace
generation throughput, never as simulated cycles.

| Spike | binary size | emitted instructions | inst/s samples | median inst/s |
|---|---:|---:|---|---:|
| inherited CVA6 fork | 243,346,160 B | 650,517 | 26,323; 25,658; 25,669 | **25,669** |
| upstream release | 15,296,432 B | 897,621 | 169,913; 166,656; 171,414 | **169,913** |

That is a measured **6.62x** throughput improvement.  Each implementation's
instruction count and final program output were stable across its three runs.
The counts differ between Spike revisions, so this is a complete-program
commit-log throughput comparison, not a claim that both simulators emit an
identical record set.  The large gain comes from changing from the CVA6 fork's
verbose debug logger to current upstream as well as from the optimized build;
it must not be attributed to stripping alone.

## Reproduction

Prerequisites are the already-established RISC-V Linux cross toolchain and the
existing `pk` configured for `rv64imafdc_zifencei`.  Network access is needed
the first time to fetch pinned Spike, Bionic, and Lua sources.  Every generated
source/build/output path is `tmp_`-prefixed.

```sh
cd c-class/perf-model/linux-traces

# Pinned upstream release Spike.
./build_spike_release.sh

# Historical scalar Bionic routines, harness, marker-bounded trace.
./fetch_bionic_subset.sh
./build_android_proxy.sh
./gen_android_proxy_trace.sh

# Managed-runtime/cache-pressure proxy.  The generator streams only the final
# 250k marked instructions, avoiding a multi-gigabyte intermediate log.
./build_lua_proxy.sh
./gen_lua_proxy_trace.sh tmp_android_lua_proxy \
  tmp_android_lua_cpressure_250k.log

# Required characterization and refusal checks.
./characterize.py tmp_android_lua_cpressure_250k.log \
  tmp_android_bionic_proxy.log
./symbol_coverage.py tmp_android_bionic_proxy.log \
  tmp_android_bionic_proxy
sha256sum tmp_android_lua_cpressure_250k.log \
  tmp_android_bionic_proxy.log
```

To repeat the throughput benchmark:

```sh
./benchmark_spike.py \
  --spike inherited=/home/blazevfx/Documents/projects/cva6/tools/spike/bin/spike \
  --spike release=tmp_official-spike-release/bin/spike \
  --pk /home/blazevfx/tracework/install/riscv64-linux-gnu/bin/pk \
  --binary /home/blazevfx/tracework/ws8 --runs 3
```

The trace filter recognizes the marker instructions in Spike's stderr commit
log and writes parser-ready commit lines only.  Target stdout is kept in the
corresponding `.out` file.  `characterize.py` exits nonzero for every
`unknown_*` or `unsupported_*` instruction unless explicitly invoked with the
diagnostic-only `--allow-unsupported` option.

## Why this is not a current Android trace

RVA23 makes V mandatory; the official profile marks V as a mandatory extension
and adds mandatory vector subsets including Zvfhmin, Zvbb, and Zvkt.  It also
makes B and several scalar subsets mandatory.  See the
[official RVA23 profile](https://github.com/riscv/riscv-profiles/blob/main/src/rva23-profile.adoc).
Current Android's Bionic change above confirms that the platform is using this
requirement rather than retaining scalar fallbacks.

The C-class model and target core have no vector execution unit.  Adding names
for vector opcodes would therefore reproduce the original silent-wrong failure:
correct timing depends on VL, SEW, LMUL, VLEN, lane count, issue/throughput and
latency, chaining, masking, and vector-memory behaviour.  A current Android
trace cannot produce meaningful cycle predictions for this scalar Shakti core.
The model now recognizes vector arithmetic/configuration and vector
load/store opcode spaces and refuses such a trace before simulation.  This is
detection-and-refusal, not vector timing support.

Vector-decoder work is avoidable only by changing the question:

1. use historical/non-conformant scalar Android-derived code, as in the Bionic
   trace above; or
2. maintain a custom non-V AOSP port and patch current Bionic, ART, compiler
   target settings, and other RVA23 assumptions.

Option 2 would not be a current conformant Android target and would still need
support for RVA23 scalar extensions absent from the model.  For a real current
Android performance result, vector *microarchitecture modeling*, not just a
decoder, is unavoidable.

### Full AOSP cost

Google's current [AOSP build requirements](https://source.android.com/docs/setup/start/requirements)
specify 400 GB free disk (250 GB checkout plus 150 GB build) and at least 64 GB
RAM.  Their published examples are approximately 40 minutes on a 72-core,
64-GB machine and 6 hours on a 6-core, 64-GB machine for a full build; source
synchronization and trace generation are additional.  This host was measured
at 27 GiB RAM, 16 logical CPUs, and 72 GB free disk before cleanup, so a full
AOSP checkout/build is infeasible here by both official memory and disk
requirements.  It was not attempted, and no unmeasured local build-time number
is presented.

An ART-interpreter-only shortcut is not a standalone C/C++ cross-build.  ART's
[runtime build definition](https://android.googlesource.com/platform/art/+/master/runtime/Android.bp)
generates the RISC-V mterp assembly from `interpreter/mterp/riscv64/*.S` with
`gen_mterp.py` and generated dex-instruction headers, then links it into the ART
runtime.  Reproducing an executable path therefore needs a substantial portion
of ART's Soong-generated build graph and runtime, not merely one source file.
Binder similarly needs Android userspace libraries and generated interfaces.
The small historical Bionic subset is the only tested standalone Android-code
slice found in this investigation.

### Full-system Linux status

The local `bbl` is only 63,976 bytes.  Its `_payload_start` and `_payload_end`
symbols delimit 410 bytes (`0x80015000` to `0x8001519a`), i.e. a dummy payload;
there is no local Linux `Image`/`vmlinux` or initramfs/rootfs to boot.  A kernel
trace would require building/providing those pieces and rebuilding bbl with
`--with-payload`.  It would add kernel, context-switch, and TLB behaviour, but
would still be Linux rather than Android and would not remove the current-AOSP
vector mismatch.  No full-system trace or speculative time estimate is claimed.
