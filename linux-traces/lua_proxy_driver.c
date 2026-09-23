/* Embed Lua so the trace can be bounded by instructions outside the workload. */
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>

#include "lauxlib.h"
#include "lua.h"
#include "lualib.h"

#define START_MARKER 0x12345037
#define END_MARKER   0x54321037

struct cache_node {
  uint64_t next;
  uint64_t payload[7];
};

static struct cache_node *cache_nodes;
static const size_t cache_node_count = 32768;

static int cache_pressure(lua_State *state) {
  uint64_t index = (uint64_t)luaL_checkinteger(state, 1);
  uint64_t sum = 0;
  for (unsigned hop = 0; hop < 8; ++hop) {
    index = cache_nodes[index].next;
    sum += cache_nodes[index].payload[(index >> 5) & 3];
  }
  lua_pushinteger(state, (lua_Integer)index);
  lua_pushinteger(state, (lua_Integer)sum);
  return 2;
}

int main(int argc, char **argv) {
  if (argc != 2) {
    fprintf(stderr, "usage: %s workload.lua\n", argv[0]);
    return 2;
  }
  lua_State *state = luaL_newstate();
  if (state == NULL) return 2;
  luaL_openlibs(state);
  if (posix_memalign((void **)&cache_nodes, 64,
                     cache_node_count * sizeof(*cache_nodes)) != 0) {
    lua_close(state);
    return 2;
  }
  for (size_t i = 0; i < cache_node_count; ++i) {
    /* Full-period LCG over a power-of-two table: a=5, c=1. */
    cache_nodes[i].next = (i * 5 + 1) & (cache_node_count - 1);
    for (size_t word = 0; word < 7; ++word) {
      cache_nodes[i].payload[word] = i * 17 + word;
    }
  }
  lua_pushcfunction(state, cache_pressure);
  lua_setglobal(state, "cache_pressure");

  __asm__ volatile(".word %0" : : "i"(START_MARKER) : "memory");
  int status = luaL_dofile(state, argv[1]);
  __asm__ volatile(".word %0" : : "i"(END_MARKER) : "memory");

  if (status != LUA_OK) {
    fprintf(stderr, "%s\n", lua_tostring(state, -1));
  }
  lua_close(state);
  free(cache_nodes);
  return status == LUA_OK ? 0 : 1;
}
