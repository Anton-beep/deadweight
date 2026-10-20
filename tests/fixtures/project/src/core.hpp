#pragma once

// Reached from a test and from nothing in production: the candidate.
int test_only_helper(int value);

// Reached from nothing at all: reported as dead anywhere.
int unused_everywhere(int value);

class Shape {
 public:
  virtual ~Shape() = default;

  virtual void draw() const = 0;
};

// The override is only ever called from a test, and virtual dispatch is invisible
// to the static graph, so it belongs in keepers rather than candidates.
class Square : public Shape {
 public:
  void draw() const override;
};
