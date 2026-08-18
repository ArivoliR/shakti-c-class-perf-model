"""Banking on C-Class-DI-64b-mod, with D-cache miss latency modelled.

The earlier banking measurement (+2.6%) modelled the issue path only: two
memory operations were allowed to start in the same cycle, but the model
priced every access at ~1 cycle regardless of whether it hit. That overstates
banking's value on any workload that misses, because a bank conflict between
two accesses that are both going to stall for 9 cycles anyway costs nothing.

This repeats the measurement with model_dcache enabled, so a miss carries the
penalty measured from the RTL trace.

"-mod" is Mouna's modified instruction scheduler (thesis Table 3.6): BRANCH may
pair with MEMORY, MULDIV or FLOAT, the second instruction executing
speculatively and being dropped in the next stage on a mispredict. The repo
already implements this -- stage2.bsv:544/546 -- and ibuswidth=64, so this
configuration is C-Class-DI-64b-mod.
"""
import sys, argparse
sys.path.insert(0, '/home/blazevfx/Documents/projects/shakti/c-class/perf-model')
from model import Model
from trace import detect_benchmark_window, parse_app_log_metrics
from trace_cache import load_or_parse_trace_files
from pathlib import Path

DUAL = Path("/home/blazevfx/Documents/projects/shakti/c-class-dual-issue")

def build(**o):
    b = dict(num_issue=2, dual_policy="shakti", model_fetch_word_alignment=True,
             fetch_width=2, fetch_decode_width=2, decode_width=1, issue_width=1,
             stage4_width=1, commit_width=1, memory_issue_width=1,
             control_issue_width=1, isb_s0s1=4, isb_s1s2=6, isb_s2s3=2,
             isb_s3s4=16, isb_s4s5=16, lockstep_bundles=True, atomic_pair_retire=True)
    b.update(o)
    return Model.from_repo(DUAL, **b)

ap = argparse.ArgumentParser()
ap.add_argument("trace"); ap.add_argument("--applog", default=None)
ap.add_argument("--label", default=""); ap.add_argument("--limit", type=int, default=400000)
ap.add_argument("--miss-penalty", type=int, default=9)
a = ap.parse_args()

e = load_or_parse_trace_files([a.trace], limit=a.limit)
if a.applog:
    m = parse_app_log_metrics(a.applog)
    w = detect_benchmark_window(e, m) if m else None
    if w:
        s = w.entries(e)
        if s: e = s
n = len(e)
print(f"{a.label}: {n:,} instructions   (C-Class-DI-64b-mod)\n", flush=True)

BANK = dict(memory_pairing="banked", memory_issue_width=2)
cfgs = [
    ("baseline", {}),
    ("banked 2", {**BANK, "mem_banks": 2}),
    ("banked 4", {**BANK, "mem_banks": 4}),
    ("banked 8", {**BANK, "mem_banks": 8}),
    ("perfect 2nd port", {"memory_pairing": "all", "memory_issue_width": 2}),
]
print(f"  {'config':>18} {'no dcache':>22} {'with dcache miss='+str(a.miss_penalty):>24}")
print(f"  {'':>18} {'cycles':>11} {'vs base':>10} {'cycles':>11} {'vs base':>12}")
b1 = b2 = None
for name, kw in cfgs:
    c1 = (lambda r: r[-1]-r[0]+1)(build(**kw).run(e))
    c2 = (lambda r: r[-1]-r[0]+1)(build(model_dcache=True,
                                        dcache_miss_penalty=a.miss_penalty, **kw).run(e))
    if b1 is None: b1, b2 = c1, c2
    print(f"  {name:>18} {c1:>11,} {100*(b1-c1)/b1:>+9.3f}% {c2:>11,} {100*(b2-c2)/b2:>+11.3f}%", flush=True)
