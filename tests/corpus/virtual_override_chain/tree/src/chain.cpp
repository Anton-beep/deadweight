#include "chain.hpp"

int Concrete::compute() const { return helper() * 2; }

int Concrete::helper() const { return 7; }

// The reference is real, the reachability is not: a defaulted member called only from
// a dead production body is dead weight exactly as that body is, and is never proposed.
bool compare_dead(const Key &left, const Key &right) { return left == right; }
