/* Pointer-chase + memcpy with a tunable working set.
   WS_KB <= 16 fits the 16KB L1 (like CoreMark/Dhrystone).
   WS_KB >> 16 misses, which is the regime Android actually runs in. */
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#ifndef WS_KB
#define WS_KB 8
#endif
#ifndef ITERS
#define ITERS 20000
#endif
typedef struct node { struct node *next; long payload[7]; } node; /* 64B = 1 line */
int main(void){
  long n = (WS_KB*1024L)/sizeof(node);
  node *a = calloc(n, sizeof(node));
  /* shuffled ring: defeats sequential prefetch, forces dependent loads */
  long *idx = malloc(n*sizeof(long));
  for(long i=0;i<n;i++) idx[i]=i;
  unsigned s=12345;
  for(long i=n-1;i>0;i--){ s=s*1103515245u+12345u; long j=(s>>16)%(i+1);
                           long t=idx[i]; idx[i]=idx[j]; idx[j]=t; }
  for(long i=0;i<n;i++) a[idx[i]].next = &a[idx[(i+1)%n]];
  node *p = &a[0]; long acc=0;
  char *src = malloc(4096), *dst = malloc(4096);
  memset(src,0xA5,4096);
  for(long i=0;i<ITERS;i++){
    p = p->next;              /* dependent load chain */
    acc += p->payload[0];
    if((i & 63)==0) memcpy(dst,src,256);   /* copy traffic */
  }
  printf("%ld %d\n", acc, dst[7]);
  return 0;
}
