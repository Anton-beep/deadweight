#include "api.hpp"

// The call site lives in another translation unit than the definition, which is the
// ordinary case and the one a per-unit graph has to join.
int production_user(int value) { return shared_core(value); }

int main() { return production_user(1); }
