#include "chain.hpp"

// The call goes through the interface, so the override it lands on has no call site
// the static graph can attribute: only dispatch keeps it alive.
int test_dispatches() {
  Concrete concrete;
  const Interface& view = concrete;
  return view.compute();
}
