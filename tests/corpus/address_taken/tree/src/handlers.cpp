#include "handlers.hpp"

int handler_alpha(int value) { return value + 1; }

int handler_beta(int value) { return value * 2; }

int handler_unused(int value) { return value - 1; }

using Handler = int (*)(int);

// The table sits at namespace scope, where no function body encloses the reference,
// so both handlers are roots: taking an address is a use the graph cannot follow.
Handler const handler_table[] = {handler_alpha, handler_beta};
