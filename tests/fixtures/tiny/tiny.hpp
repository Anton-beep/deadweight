#pragma once

class Widget {
 public:
  Widget() = default;
  Widget(const Widget &other) = default;
  virtual ~Widget();

  Widget &operator=(const Widget &other);
  explicit operator bool() const;

  virtual void draw() const;
  virtual void update() const = 0;
  void forbid() = delete;
  int width() const { return 4; }
};

class Panel : public Widget {
 public:
  void update() const override;
};

int free_function(int value);

int dead_helper(int value);

class Reader {
 public:
  virtual int read() const = 0;
};
