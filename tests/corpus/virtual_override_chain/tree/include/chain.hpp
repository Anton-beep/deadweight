#pragma once

class Interface {
 public:
  virtual ~Interface() = default;

  virtual int compute() const = 0;
};

class Concrete : public Interface {
 public:
  int compute() const override;

 private:
  int helper() const;
};
