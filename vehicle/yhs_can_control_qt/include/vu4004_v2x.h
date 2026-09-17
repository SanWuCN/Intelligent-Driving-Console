#ifndef VU4004_V2X_H
#define VU4004_V2X_H

#include "cJSON.h"
#include <QDebug>
#include <QElapsedTimer>
#include <QImage>
#include <QObject>
#include <arpa/inet.h>
#include <opencv2/opencv.hpp>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <thread>
#include <atomic>
#include <unistd.h>

#define MAX_OTHER_VEH 100

// NCS 响应注册
typedef struct {
  int rsp;
  char *detail;
  char *unique;
} register_respond;

// HMI 客户端解注册请求
typedef struct {
  char *unique;
} unregister_req;

// HMI 激活请求
typedef struct {
  char *unique;
} active_req;

// HMI 本车节点信息上报
typedef struct {
  char *device_id;
  int vehicle_type;
  int vip_status;
  char *vehicle_num;
  int drive_status;
  double lon;
  double lat;
  double ele;
  double hea;
  double spd;
  bool pos_valid;
  int emergencyStatus;
  int absActivate;
  int outofControl;
  int gnsStatus;
  bool V2xCover;
  long current_time;
} vehicle_info;

// HMI 附近其他车辆节点信息上报
typedef struct {
  int flag;
  char *device_id;
  int vehicle_type;
  char *vehicle_num;
  int security;
  double lon;
  double lat;
  double ele;
  double hea;
  double spd;
} other_vehicle_info;

// 自定义消息
typedef struct {
  int src_dev;

  int traffic_light_phase;
  int traffic_light_sec;

  int event_type;
  int event_radius;
  double event_lat;
  double event_lon;

  int spd_limit_value;

  int abnormal_veh;
  int out_of_ctrl_veh;

  int traffic_participants;
} rsi;

class vu4004_v2x : public QObject {
  Q_OBJECT

public:
  vu4004_v2x();
  ~vu4004_v2x();

private:
  int sockfd = -1;
  std::atomic<bool> running{false};
  struct sockaddr_in hmi_addr;
  int hmi_addr_len;

  // NCS地址
  struct sockaddr_in ncs_addr;
  int ncs_addr_len;

  // 数据
  register_respond register_respond_ctx;
  unregister_req unregister_req_ctx;
  active_req active_req_ctx;
  vehicle_info vehicle_info_ctx;
  other_vehicle_info other_vehicle_info_ctx[MAX_OTHER_VEH];
  rsi rsi_data;
  QElapsedTimer ego_position_age;
  QElapsedTimer other_position_age;
  bool positions_valid() const;

  // 摄像头
  cv::VideoCapture *cam_capture = nullptr;

  void process();
  std::thread *process_thd = nullptr;

public slots:
  void send_register_req();
  void send_unregister_req();
  void send_active_req();

  void parse_register_respond(cJSON *);
  void parse_vehicle_info(cJSON *);
  void parse_other_vehicle_info(cJSON *);
  void parse_rsi_data(cJSON *);

  double llh2dist(double lat, double lon, double lat_origin, double lon_origin);

signals:
  void parse_vehicle_info_ok(vehicle_info ctx);
  void parse_other_vehicle_info_ok();
  void parse_rsi_data_ok(rsi data);

  void cam_frame_ok(QImage img);

  void fcw_trigger(bool en);
  void fbw_trigger(bool en);
};

#endif // VU4004_V2X_H
