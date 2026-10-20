#include "recurse.hpp"

// Recursion is a call edge like any other: the only caller is the function itself, so
// it never becomes a root.
int countdown(int value) { return value <= 0 ? 0 : countdown(value - 1); }
