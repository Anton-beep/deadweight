#pragma once

int transform(int value);

// The body of a template is not walked by the front end, so the call it makes to
// transform is invisible to the graph. The production file that mentions the name is
// what the report adds, and a human decides.
template <typename T>
T doubled(T value) {
  return transform(value) * 2;
}

int run_pipeline(int value);
