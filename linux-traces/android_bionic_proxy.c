/*
 * Cache-stressing harness for the last non-vector RISC-V Bionic string path.
 *
 * The bionic_* functions are compiled verbatim from AOSP revision f971dc6b.
 * This file is a workload driver, not Android code.  It deliberately dispatches
 * through function pointers and touches pseudo-random cache lines in a working
 * set much larger than the C-class 16 KiB D-cache.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#ifndef ARENA_MIB
#define ARENA_MIB 1
#endif

#ifndef ITERATIONS
#define ITERATIONS 10000
#endif

#define START_MARKER 0x12345037
#define END_MARKER   0x54321037

extern void *bionic_memcpy(void *, const void *, size_t);
extern void *bionic_memmove(void *, const void *, size_t);
extern void *bionic_memset(void *, int, size_t);
extern int bionic_memcmp(const void *, const void *, size_t);

struct arenas {
  unsigned char *source;
  unsigned char *shadow;
  unsigned char *scratch;
};

typedef uint64_t (*operation)(struct arenas *, size_t, size_t, uint64_t);

__attribute__((noinline, noclone))
static uint64_t copy_op(struct arenas *a, size_t offset, size_t length,
                        uint64_t seed) {
  unsigned char *result = bionic_memcpy(a->scratch + offset,
                                        a->source + offset, length);
  return result[(seed >> 16) % length];
}

__attribute__((noinline, noclone))
static uint64_t move_op(struct arenas *a, size_t offset, size_t length,
                        uint64_t seed) {
  size_t displacement = 1 + ((seed >> 20) & 15);
  unsigned char *result = bionic_memmove(a->scratch + offset + displacement,
                                         a->scratch + offset, length);
  return result[(seed >> 12) % length];
}

__attribute__((noinline, noclone))
static uint64_t set_op(struct arenas *a, size_t offset, size_t length,
                       uint64_t seed) {
  unsigned char *result = bionic_memset(a->scratch + offset, (int)seed, length);
  return result[(seed >> 8) % length];
}

__attribute__((noinline, noclone))
static uint64_t compare_op(struct arenas *a, size_t offset, size_t length,
                           uint64_t seed) {
  return (uint64_t)bionic_memcmp(a->source + offset, a->shadow + offset, length)
         + (seed & 1);
}

static uint64_t next_random(uint64_t *state) {
  uint64_t x = *state;
  x ^= x << 13;
  x ^= x >> 7;
  x ^= x << 17;
  *state = x;
  return x;
}

int main(void) {
  const size_t arena_bytes = (size_t)ARENA_MIB * 1024 * 1024;
  const size_t alignment = 64;
  struct arenas a;
  if (posix_memalign((void **)&a.source, alignment, arena_bytes) != 0 ||
      posix_memalign((void **)&a.shadow, alignment, arena_bytes) != 0 ||
      posix_memalign((void **)&a.scratch, alignment, arena_bytes) != 0) {
    return 2;
  }

  /* Keep pre-marker initialization short: Spike pays the logging cost even
   * for records discarded by the marker filter.  One MiB is still 64x L1. */
  bionic_memset(a.source, 0x5a, arena_bytes);
  bionic_memcpy(a.shadow, a.source, arena_bytes);
  bionic_memset(a.scratch, 0xa5, arena_bytes);
  uint64_t state = UINT64_C(0x243f6a8885a308d3);

  /* Each entry is a real indirect call; the table also controls the mix. */
  static operation operations[] = {
      copy_op, compare_op, move_op, copy_op,
      set_op, compare_op, copy_op, move_op,
  };
  const size_t operation_count = sizeof(operations) / sizeof(operations[0]);
  volatile uint64_t checksum = 0;

  __asm__ volatile(".word %0" : : "i"(START_MARKER) : "memory");
  for (size_t i = 0; i < ITERATIONS; ++i) {
    uint64_t random = next_random(&state);
    size_t line_count = (arena_bytes - 512) / 64;
    size_t offset = (size_t)(random % line_count) * 64;
    size_t length = 32 + (size_t)((random >> 24) & 7) * 16;
    operation op = operations[(random >> 40) % operation_count];
    checksum += op(&a, offset, length, random);
  }
  __asm__ volatile(".word %0" : : "i"(END_MARKER) : "memory");

  printf("checksum=%llu iterations=%d arena_mib=%d\n",
         (unsigned long long)checksum, ITERATIONS, ARENA_MIB);
  free(a.scratch);
  free(a.shadow);
  free(a.source);
  return 0;
}
