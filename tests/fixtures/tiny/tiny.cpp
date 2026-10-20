#include "tiny.hpp"

namespace {

int file_local(int value) { return value * 2; }

}  // namespace

int free_function(int value) { return file_local(value) + 1; }

int dead_helper(int value) { return value - 1; }

Widget::~Widget() = default;

Widget &Widget::operator=(const Widget &other) { return *this; }

Widget::operator bool() const { return true; }

void Widget::draw() const {}

void Panel::update() const {}

int caller() { return free_function(3); }

int Reader::read() const { return 0; }
