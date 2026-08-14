import sys; sys.path.insert(0,'/home/blazevfx/Documents/projects/shakti/c-class/perf-model')
from model import Model
from trace import detect_benchmark_window, parse_app_log_metrics
from trace_cache import load_or_parse_trace_files
from pathlib import Path
DUAL=Path("/home/blazevfx/Documents/projects/shakti/c-class-dual-issue")
def build(**o):
    b=dict(num_issue=2,dual_policy="shakti",model_fetch_word_alignment=True,fetch_width=2,
           fetch_decode_width=2,decode_width=1,issue_width=1,stage4_width=1,commit_width=1,
           memory_issue_width=1,control_issue_width=1,isb_s0s1=4,isb_s1s2=6,isb_s2s3=2,
           isb_s3s4=16,isb_s4s5=16,lockstep_bundles=True,atomic_pair_retire=True)
    b.update(o); return Model.from_repo(DUAL,**b)
import argparse
ap=argparse.ArgumentParser(); ap.add_argument("trace"); ap.add_argument("--applog",default=None)
ap.add_argument("--label",default=""); ap.add_argument("--limit",type=int,default=400000)
a=ap.parse_args()
e=load_or_parse_trace_files([a.trace],limit=a.limit)
if a.applog:
    m=parse_app_log_metrics(a.applog); w=detect_benchmark_window(e,m) if m else None
    if w:
        s=w.entries(e)
        if s: e=s
n=len(e)
cfgs=[("baseline (1 mem port)",{}),
      ("banked  2, line-intlv",{"memory_pairing":"banked","memory_issue_width":2,"mem_banks":2}),
      ("banked  4, line-intlv",{"memory_pairing":"banked","memory_issue_width":2,"mem_banks":4}),
      ("banked  8, line-intlv",{"memory_pairing":"banked","memory_issue_width":2,"mem_banks":8}),
      ("banked  4 + line-merge",{"memory_pairing":"banked","memory_issue_width":2,"mem_banks":4,"bank_merge_same_line":True}),
      ("banked  8 + line-merge",{"memory_pairing":"banked","memory_issue_width":2,"mem_banks":8,"bank_merge_same_line":True}),
      ("PERFECT 2nd port",{"memory_pairing":"all","memory_issue_width":2})]
print(f"{a.label}: {n:,} instructions\n")
base=None
print(f"  {'config':>24} {'cycles':>11} {'IPC':>8} {'vs base':>9}")
for name,kw in cfgs:
    c=(lambda r:r[-1]-r[0]+1)(build(**kw).run(e))
    if base is None: base=c
    print(f"  {name:>24} {c:>11,} {n/c:>8.4f} {100*(base-c)/base:>+8.3f}%")
