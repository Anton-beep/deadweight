#pragma once

// A body that lives in a header and is reached only from a test: no production
// translation unit can keep it alive, and the linker cannot see it either once the
// optimizer inlines it.
inline int inline_helper(int value) { return value * 3; }
