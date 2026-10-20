#pragma once

class Box {
 public:
  explicit Box(int value);
  int value() const;

 private:
  int value_;
};
