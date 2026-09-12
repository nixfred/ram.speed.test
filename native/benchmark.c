#define _GNU_SOURCE
#include <errno.h>
#include <math.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>

void copy_kernel(double *, const double *, size_t);
void triad_kernel(double *, const double *, const double *, size_t);

static pthread_barrier_t ready, done;
static int stop, triad;
static size_t elements;
static double *arrays[CPU_SETSIZE][3];
static int cpus[CPU_SETSIZE];

static void fail(const char *message)
{
    fprintf(stderr, "%s\n", message);
    exit(1); /* Also releases any threads waiting at a barrier. */
}

static double now(void)
{
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) fail("clock_gettime failed");
    return t.tv_sec + t.tv_nsec / 1e9;
}

static void *worker(void *arg)
{
    size_t id = (size_t)arg;
    cpu_set_t mask;
    CPU_ZERO(&mask);
    CPU_SET(cpus[id], &mask);
    if (pthread_setaffinity_np(pthread_self(), sizeof(mask), &mask))
        fail("Could not pin a worker to its selected CPU");
    /* Allocate and first-touch on the worker: local placement under the
       inherited OS memory policy. No root privileges or NUMA policy changes. */
    for (int k = 0; k < 3; ++k) {
        arrays[id][k] = mmap(NULL, elements * sizeof(double),
                            PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS,
                            -1, 0);
        if (arrays[id][k] == MAP_FAILED) fail("Could not allocate worker arrays");
    }
    double *a = arrays[id][0], *b = arrays[id][1], *c = arrays[id][2];
    for (size_t i = 0; i < elements; ++i) {
        a[i] = 0.0;
        b[i] = 1.0 + (double)((i + id) % 1024) / 1024.0;
        c[i] = 2.0 + (double)((i + 3 * id) % 512) / 512.0;
    }
    for (;;) {
        pthread_barrier_wait(&ready);
        if (stop) break;
        if (triad) triad_kernel(a, b, c, elements);
        else copy_kernel(a, b, elements);
        pthread_barrier_wait(&done);
    }
    /* Full output validation outside the measured interval. */
    for (size_t i = 0; i < elements; ++i) {
        double expected = triad ? b[i] + 3.0 * c[i] : b[i];
        if (a[i] != expected) fail("Output validation failed");
    }
    for (int k = 0; k < 3; ++k)
        munmap(arrays[id][k], elements * sizeof(double));
    return NULL;
}

static unsigned long long integer(const char *s)
{
    char *end;
    errno = 0;
    unsigned long long value = strtoull(s, &end, 10);
    if (errno || !*s || *s == '-' || *end) fail("Invalid integer argument");
    return value;
}

static double duration(const char *s, int allow_zero)
{
    char *end;
    double value = strtod(s, &end);
    if (!*s || *end || !isfinite(value) || value < (allow_zero ? 0.0 : 0.01)
        || value > 3600.0) fail("Invalid duration argument");
    return value;
}

int main(int argc, char **argv)
{
    if (argc != 6) fail("Usage: benchmark copy|triad total_bytes cpu_list seconds warmup");
    pid_t parent = getppid();
    if (prctl(PR_SET_PDEATHSIG, SIGTERM)) fail("Could not set parent-death signal");
    if (getppid() != parent || parent == 1) return 1;
    if (strcmp(argv[1], "copy") && strcmp(argv[1], "triad")) fail("Unknown kernel");
    triad = !strcmp(argv[1], "triad");
    unsigned long long bytes = integer(argv[2]);
    double seconds = duration(argv[4], 0), warmup = duration(argv[5], 1);
    cpu_set_t allowed, selected;
    CPU_ZERO(&selected);
    if (sched_getaffinity(0, sizeof(allowed), &allowed)) fail("Could not read CPU affinity");
    int count = 0;
    for (char *p = strtok(argv[3], ","); p; p = strtok(NULL, ",")) {
        unsigned long long cpu = integer(p);
        if (cpu >= CPU_SETSIZE || count >= CPU_SETSIZE ||
            !CPU_ISSET(cpu, &allowed) || CPU_ISSET(cpu, &selected))
            fail("Invalid, duplicate, or unavailable CPU");
        CPU_SET(cpu, &selected);
        cpus[count++] = (int)cpu;
    }
    if (!count || bytes > SIZE_MAX || bytes < (unsigned long long)count * 3 * 4096)
        fail("Invalid allocation size");
    /* Whole pages, rounded DOWN so requested memory is never exceeded. */
    size_t page = (size_t)sysconf(_SC_PAGESIZE);
    elements = (size_t)(bytes / (3 * count) / page * page / sizeof(double));
    if (!elements) fail("Allocation too small");
    unsigned long long pass_bytes = (unsigned long long)elements * count *
                                   (triad ? 3 : 2) * sizeof(double);
    pthread_t threads[CPU_SETSIZE];
    if (pthread_barrier_init(&ready, NULL, count + 1) ||
        pthread_barrier_init(&done, NULL, count + 1)) fail("Barrier initialization failed");
    for (int i = 0; i < count; ++i)
        if (pthread_create(&threads[i], NULL, worker, (void *)(size_t)i))
            fail("Could not start worker");

    /* Initial pass waits for first-touch and is never timed. */
    pthread_barrier_wait(&ready);
    pthread_barrier_wait(&done);
    double warm_start = now();
    do {
        pthread_barrier_wait(&ready);
        pthread_barrier_wait(&done);
    } while (now() - warm_start < warmup);
    struct rusage before, after;
    if (getrusage(RUSAGE_SELF, &before)) fail("getrusage failed");
    double start = now(), last = start, t = start;
    unsigned long long passes = 0, last_passes = 0;
    do {
        pthread_barrier_wait(&ready);
        pthread_barrier_wait(&done);
        ++passes;
        t = now();
        if (t - last >= 1.0 || t - start >= seconds) {
            printf("{\"type\":\"sample\",\"seconds\":%.9f,\"bytes\":%llu}\n",
                   t - last, (passes - last_passes) * pass_bytes);
            fflush(stdout);
            last = t;
            last_passes = passes;
        }
    } while (t - start < seconds);
    if (getrusage(RUSAGE_SELF, &after)) fail("getrusage failed");
    stop = 1;
    pthread_barrier_wait(&ready);
    for (int i = 0; i < count; ++i) pthread_join(threads[i], NULL);
    printf("{\"type\":\"result\",\"seconds\":%.9f,\"bytes\":%llu,"
           "\"allocated_bytes\":%llu,\"major_faults\":%ld,\"minor_faults\":%ld,"
           "\"validated\":true}\n", t - start, passes * pass_bytes,
           (unsigned long long)elements * count * 3 * sizeof(double),
           after.ru_majflt - before.ru_majflt, after.ru_minflt - before.ru_minflt);
    return 0;
}
