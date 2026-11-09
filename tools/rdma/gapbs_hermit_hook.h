// Hermit swap measurement hook for an unmodified GAPBS checkout.
//
// Injected with: g++ -I <gapbs>/src -include gapbs_hermit_hook.h <gapbs>/src/bfs.cc
// The kernel binary then follows the protocol of run_gapbs_page_sweep.sh:
//   1. builds the graph as usual, then prints  READY pid=<pid> nodes=<n> edges=<m>
//      and waits; the driver lowers memory.max so part of the graph swaps out;
//   2. on SIGUSR2 prints RUN, runs the original BenchmarkKernel (trials and
//      verification exactly as GAPBS does them), then prints DONE sec=<sec>;
//   3. waits for a second SIGUSR2 so the driver can snapshot a live cgroup,
//      then returns from main().
#ifndef HERMIT_GAPBS_HOOK_H_
#define HERMIT_GAPBS_HOOK_H_

#include <csignal>
#include <cstdio>
#include <ctime>
#include <pthread.h>
#include <unistd.h>

#include "benchmark.h"

namespace hermit_gapbs {

inline sigset_t control_signals() {
  sigset_t set;
  sigemptyset(&set);
  sigaddset(&set, SIGUSR2);
  return set;
}

// Static initialisation runs before main(), so every OpenMP thread created
// during graph construction inherits the mask: SIGUSR2 is only ever consumed
// by sigwait() and can never terminate the process from another thread.
struct Init {
  Init() {
    sigset_t set = control_signals();
    pthread_sigmask(SIG_BLOCK, &set, nullptr);
    setvbuf(stdout, nullptr, _IOLBF, 0);
  }
};
static Init init;

inline void wait_for_driver() {
  sigset_t set = control_signals();
  int sig = 0;
  while (sigwait(&set, &sig) != 0) {
  }
}

inline double monotonic_seconds() {
  timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return t.tv_sec + t.tv_nsec * 1e-9;
}

template <typename GraphT_, typename GraphFunc, typename AnalysisFunc,
          typename VerifierFunc>
void Run(const CLApp &cli, const GraphT_ &g, GraphFunc kernel,
         AnalysisFunc stats, VerifierFunc verify) {
  std::printf("READY pid=%ld nodes=%lld edges=%lld\n",
              static_cast<long>(getpid()),
              static_cast<long long>(g.num_nodes()),
              static_cast<long long>(g.num_edges()));
  std::fflush(stdout);
  wait_for_driver();
  std::printf("RUN\n");
  std::fflush(stdout);
  double start = monotonic_seconds();
  BenchmarkKernel(cli, g, kernel, stats, verify);  // the original template
  std::printf("DONE sec=%.6f\n", monotonic_seconds() - start);
  std::fflush(stdout);
  wait_for_driver();
}

}  // namespace hermit_gapbs

// benchmark.h is already included and guarded, so every later call in the
// kernel's main() resolves to the wrapper above.
#define BenchmarkKernel hermit_gapbs::Run

#endif  // HERMIT_GAPBS_HOOK_H_
