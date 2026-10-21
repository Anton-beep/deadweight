// One invocation defines both twins, so a report can only point at the same site and
// no name-based matching can tell the two definitions apart. Two *separate*
// invocations of the same macro are a different shape: each has its own site, and the
// scan reports them as ordinary functions rather than as an ambiguity.
#define DEFINE_TWINS(first, second)          \
  int first(int value) { return value + 1; } \
  int second(int value) { return value + 1; }

DEFINE_TWINS(twin_alpha, twin_beta)
