#include "core.hpp"

int helper_call_site() { return test_only_helper(4); }

void draw_a_square() {
  Square square;
  square.draw();
}
