#include "vu4004_v2x.h"
#include <QCoreApplication>
#include <atomic>
#include <chrono>
#include <iostream>
#include <stdexcept>

static void require(bool condition, const char *message) {
  if (!condition) throw std::runtime_error(message);
}

int main(int argc, char **argv) {
  QCoreApplication app(argc, argv);
  std::atomic<int> active{0}, cleared{0};
  vu4004_v2x v2x;
  QObject::connect(&v2x, &vu4004_v2x::fcw_trigger, &app, [&](bool value) {
    if (value) ++active;
    else ++cleared;
  }, Qt::DirectConnection);
  const int fd = socket(AF_INET, SOCK_DGRAM, 0);
  sockaddr_in address = {};
  address.sin_family = AF_INET;
  address.sin_port = htons(50500);
  address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  auto send = [&](const char *json) {
    require(sendto(fd, json, strlen(json), 0, reinterpret_cast<sockaddr *>(&address),
                   sizeof(address)) >= 0, "UDP send failed");
    std::this_thread::sleep_for(std::chrono::milliseconds(120));
  };
  std::this_thread::sleep_for(std::chrono::milliseconds(250));
  require(active == 0, "Missing OBU data caused a warning");
  send(R"({"tag":2101,"data":{"lat":31.2,"lon":121.4,"pos_valid":1}})");
  send(R"({"tag":2102,"data":[]})");
  send(R"({"tag":2102,"data":[{"lat":0,"lon":0}]})");
  require(active == 0, "Empty or zero target caused a warning");
  send(R"({"tag":2102,"data":[{"lat":31.2,"lon":121.4}]})");
  require(active == 1, "Fresh nearby target did not warn");
  send(R"({"tag":2102,"data":[{"lat":31.2,"lon":121.4}]})");
  require(active == 1, "Unchanged warning emitted repeatedly");
  const int before_timeout = cleared;
  std::this_thread::sleep_for(std::chrono::milliseconds(2200));
  require(cleared == before_timeout + 1, "Stale position did not clear warning");
  send(R"({"tag":2101,"data":{"lat":31.2,"lon":121.4,"pos_valid":0}})");
  send(R"({"tag":2102,"data":[{"lat":31.2,"lon":121.4}]})");
  require(active == 1, "Invalid ego position caused a warning");
  send(R"({"tag":2102,"data":[]})");
  send(R"({"tag":2101,"data":{"lat":31.2,"lon":121.4,"pos_valid":1}})");
  send(R"({"tag":2102,"data":[{"lat":31.201,"lon":121.4}]})");
  require(active == 1, "Distant target caused a warning");
  close(fd);
  std::cout << "PASS: missing, empty, zero, nearby, unchanged, stale, invalid and distant positions\n";
}
