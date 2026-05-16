#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

#define SYS_RESET_SWAP_STAT 471
#define SYS_GET_SWAP_STATS 472

static int hermit_reset_swap_stats(void) {
  return (int)syscall(SYS_RESET_SWAP_STAT);
}

static int hermit_get_swap_stats(int *ondemand, int *prefetch,
                                 int *hit_on_cache) {
  return (int)syscall(SYS_GET_SWAP_STATS, ondemand, prefetch, hit_on_cache);
}

int main(int argc, char **argv) {
  const char *action = "stats";
  const char *label = "default";

  if (argc >= 2)
    action = argv[1];
  if (argc >= 3)
    label = argv[2];

  if (strcmp(action, "reset") == 0) {
    if (hermit_reset_swap_stats() != 0) {
      fprintf(stderr, "reset_swap_stats failed: %s\n", strerror(errno));
      return 1;
    }

    printf("HERMIT_SWAP_STATS: action=reset label=%s rc=0\n", label);
    return 0;
  }

  if (strcmp(action, "stats") == 0) {
    int ondemand = 0;
    int prefetch = 0;
    int hit_on_cache = 0;

    if (hermit_get_swap_stats(&ondemand, &prefetch, &hit_on_cache) != 0) {
      fprintf(stderr, "get_swap_stats failed: %s\n", strerror(errno));
      return 1;
    }

    printf("HERMIT_SWAP_STATS: action=stats label=%s ondemand=%d prefetch=%d "
           "hit_on_swap_cache=%d\n",
           label, ondemand, prefetch, hit_on_cache);
    return 0;
  }

  fprintf(stderr, "usage: %s [reset|stats] [label]\n", argv[0]);
  return 2;
}
