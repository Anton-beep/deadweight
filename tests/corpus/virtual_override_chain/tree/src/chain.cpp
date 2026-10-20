#include "chain.hpp"

int Concrete::compute() const { return helper() * 2; }

int Concrete::helper() const { return 7; }
