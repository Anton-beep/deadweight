// Both twins expand from one macro body, so they share a definition anchor and no
// name-based matching can tell them apart.
#define DEFINE_TWIN(name) int name(int value) { return value + 1; }

DEFINE_TWIN(twin_alpha)
DEFINE_TWIN(twin_beta)
