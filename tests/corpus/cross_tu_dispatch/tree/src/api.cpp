#include "api.hpp"

int shared_core(int value) { return value + 1; }

int test_only_entry(int value) { return value * 2; }
