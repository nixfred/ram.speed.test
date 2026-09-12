/* Separate translation unit, without LTO: every timed pass really executes. */
#include <stddef.h>
#include <string.h>

void copy_kernel(double *restrict a, const double *restrict b, size_t n)
{
    memcpy(a, b, n * sizeof(*a));
}

void triad_kernel(double *restrict a, const double *restrict b,
                  const double *restrict c, size_t n)
{
    for (size_t i = 0; i < n; ++i)
        a[i] = b[i] + 3.0 * c[i];
}
