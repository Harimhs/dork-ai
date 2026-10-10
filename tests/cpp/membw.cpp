// Memory bandwidth test: how fast can this laptop stream RAM into the CPU?
// Writing one token with a local LLM means reading every model weight once, so
//   max tokens/s  ~=  read bandwidth (GB/s)  /  model size (GB)
// Build: g++ -O3 -march=native -fopenmp membw.cpp -o membw.exe
#include <immintrin.h>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <thread>
#include <vector>

static double now() {
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

// Each thread sums its slice with 512-bit loads (what the AVX-512 kernels in llama.cpp do).
static void read_slice(const char* p, size_t n, double* out) {
    __m512i acc0 = _mm512_setzero_si512(), acc1 = acc0, acc2 = acc0, acc3 = acc0;
    for (size_t i = 0; i + 256 <= n; i += 256) {
        acc0 = _mm512_add_epi64(acc0, _mm512_loadu_si512((const void*)(p + i)));
        acc1 = _mm512_add_epi64(acc1, _mm512_loadu_si512((const void*)(p + i + 64)));
        acc2 = _mm512_add_epi64(acc2, _mm512_loadu_si512((const void*)(p + i + 128)));
        acc3 = _mm512_add_epi64(acc3, _mm512_loadu_si512((const void*)(p + i + 192)));
    }
    acc0 = _mm512_add_epi64(_mm512_add_epi64(acc0, acc1), _mm512_add_epi64(acc2, acc3));
    *out = (double)_mm512_reduce_add_epi64(acc0);  // keep the loop from being optimised away
}

int main(int argc, char** argv) {
    size_t mb = argc > 1 ? (size_t)atol(argv[1]) : 1024;  // working set, MB (bigger than the 6 MB cache)
    double model_gb = argc > 2 ? atof(argv[2]) : 1.93;    // qwen2.5:3b Q4_K_M file size
    size_t n = mb << 20;
    char* buf = (char*)_mm_malloc(n, 64);
    memset(buf, 1, n);  // touch every page so it is really in RAM

    for (int threads : {1, 2, 4, 8}) {
        double best = 0;
        for (int rep = 0; rep < 5; rep++) {
            std::vector<std::thread> pool;
            std::vector<double> sink(threads);
            size_t slice = (n / threads) & ~(size_t)255;
            double t0 = now();
            for (int t = 0; t < threads; t++) pool.emplace_back(read_slice, buf + t * slice, slice, &sink[t]);
            for (auto& th : pool) th.join();
            double gbps = (double)slice * threads / (now() - t0) / 1e9;
            if (gbps > best) best = gbps;
        }
        printf("%d thread%s: read %.1f GB/s  -> ceiling for a %.2f GB model: %.1f tokens/s\n",
               threads, threads > 1 ? "s" : " ", best, model_gb, best / model_gb);
    }
    _mm_free(buf);
    return 0;
}
