#include "vu4004_v2x.h"
#include "cJSON.h"
#include <QElapsedTimer>
#include <iostream>
#include <opencv2/core/types.hpp>
#include <opencv2/imgproc.hpp>
#include <stdio.h>
#include <cmath>

#define STR(str) #str
// 大唐OBU的网络参数
#define NCS_ETH_IP STR(192.168.20.199)
#define NCS_RNDIS_IP STR(192.168.62.199)
#define NCS_SSID_IP STR(192.168.1.2)
#define NCS_SSID_PASSWD STR(12345678)
#define NCS_PORT 50500
// HMI的网络参数
#define HMI_IP STR(192.168.20.200)
#define HMI_PORT 50500

#define JSON_STR_MAX_SIZE 10000

#define TRAFFIC_LIGHT_LAT 31.392746
#define TRAFFIC_LIGHT_LON 121.2297789

vu4004_v2x::vu4004_v2x() {
  sockfd = socket(AF_INET, SOCK_DGRAM | SOCK_NONBLOCK, 0);
  if (sockfd < 0) {
    perror("failed to get sockfd");
    return;
  }
  memset(&hmi_addr, 0, sizeof(struct sockaddr_in));
  hmi_addr.sin_family = AF_INET;
  // The optional OBU address need not be assigned to the vehicle computer.
  hmi_addr.sin_addr.s_addr = htonl(INADDR_ANY);
  hmi_addr.sin_port = htons(HMI_PORT);
  hmi_addr_len = sizeof(struct sockaddr_in);
  if (bind(sockfd, (struct sockaddr *)&hmi_addr, hmi_addr_len)) {
    perror("failed to bind");
    close(sockfd);
    sockfd = -1;
    return;
  }

  // NCS地址
  memset(&ncs_addr, 0, sizeof(struct sockaddr_in));
  ncs_addr.sin_family = AF_INET;
  ncs_addr.sin_addr.s_addr = inet_addr(NCS_ETH_IP);
  ncs_addr.sin_port = htons(NCS_PORT);
  ncs_addr_len = sizeof(struct sockaddr_in);

  memset(&register_respond_ctx, 0, sizeof(register_respond_ctx));
  memset(&unregister_req_ctx, 0, sizeof(unregister_req_ctx));
  memset(&active_req_ctx, 0, sizeof(active_req_ctx));
  memset(&vehicle_info_ctx, 0, sizeof(vehicle_info_ctx));
  memset(&other_vehicle_info_ctx, 0, sizeof(other_vehicle_info_ctx));
  memset(&rsi_data, 0, sizeof(rsi_data));

  // 设置摄像头
  cam_capture = new cv::VideoCapture;
  if (access("/dev/video10", F_OK) == 0)
    cam_capture->open("/dev/video10", cv::CAP_ANY);
  // cam_capture->set(cv::CAP_PROP_FRAME_WIDTH, 640);
  // cam_capture->set(cv::CAP_PROP_FRAME_HEIGHT, 480);
  // cam_capture->set(cv::CAP_PROP_FPS, 25);
  // cam_capture->set(cv::CAP_PROP_FOURCC,
  //                  cv::VideoWriter::fourcc('Y', 'U', 'Y', 'V'));

  running = true;
  process_thd = new std::thread(&vu4004_v2x::process, this);
}

vu4004_v2x::~vu4004_v2x() {
  running = false;
  if (process_thd && process_thd->joinable())
    process_thd->join();
  delete process_thd;
  delete cam_capture;
  close(sockfd);
}

void vu4004_v2x::send_register_req() {
  if (sockfd < 0) return;
  cJSON *root;
  cJSON *data;
  char *json_str = NULL;

  root = cJSON_CreateObject();
  cJSON_AddNumberToObject(root, "tag", 2001);
  data = cJSON_CreateObject();
  cJSON_AddItemToObject(root, "data", data);
  // Discover the local address used to reach the OBU for its reply address.
  struct sockaddr_in local_addr = {};
  socklen_t local_len = sizeof(local_addr);
  int route_socket = socket(AF_INET, SOCK_DGRAM, 0);
  if (route_socket < 0 ||
      ::connect(route_socket, (struct sockaddr *)&ncs_addr, ncs_addr_len) < 0 ||
      getsockname(route_socket, (struct sockaddr *)&local_addr, &local_len) < 0) {
    if (route_socket >= 0) close(route_socket);
    cJSON_Delete(root);
    return;
  }
  close(route_socket);
  cJSON_AddStringToObject(data, "ip", inet_ntoa(local_addr.sin_addr));
  cJSON_AddNumberToObject(data, "port", HMI_PORT);

  json_str = cJSON_Print(root);
  sendto(sockfd, json_str, strlen(json_str), 0, (struct sockaddr *)&ncs_addr,
         ncs_addr_len);

  cJSON_Delete(root);
  free(json_str);
}

void vu4004_v2x::send_unregister_req() {
  if (sockfd < 0 || !unregister_req_ctx.unique) return;
  cJSON *root;
  char *json_str = NULL;

  root = cJSON_CreateObject();
  cJSON_AddNumberToObject(root, "tag", 2003);
  cJSON_AddStringToObject(root, "unique", unregister_req_ctx.unique);

  json_str = cJSON_Print(root);
  sendto(sockfd, json_str, strlen(json_str), 0, (struct sockaddr *)&ncs_addr,
         ncs_addr_len);

  cJSON_Delete(root);
  free(json_str);
}

void vu4004_v2x::send_active_req() {
  if (sockfd < 0 || !active_req_ctx.unique) return;
  cJSON *root;
  char *json_str = NULL;

  root = cJSON_CreateObject();
  cJSON_AddNumberToObject(root, "tag", 2005);
  cJSON_AddStringToObject(root, "unique", active_req_ctx.unique);

  json_str = cJSON_Print(root);
  sendto(sockfd, json_str, strlen(json_str), 0, (struct sockaddr *)&ncs_addr,
         ncs_addr_len);

  cJSON_Delete(root);
  free(json_str);
}

void vu4004_v2x::parse_register_respond(cJSON *root) {
  cJSON *item;
  item = cJSON_GetObjectItem(root, "rsp");
  if (item)
    register_respond_ctx.rsp = item->valueint;
  item = cJSON_GetObjectItem(root, "detial");
  if (item)
    register_respond_ctx.detail = item->valuestring;
  item = cJSON_GetObjectItem(root, "unique");
  if (item)
    register_respond_ctx.unique = item->valuestring;
}

void vu4004_v2x::parse_vehicle_info(cJSON *root) {
  cJSON *data, *item_in_data;
  data = cJSON_GetObjectItem(root, "data");
  ego_position_age.invalidate();
  const cJSON *lat = cJSON_GetObjectItem(data, "lat");
  const cJSON *lon = cJSON_GetObjectItem(data, "lon");
  const cJSON *valid = cJSON_GetObjectItem(data, "pos_valid");
  if (lat && lon && valid && lat->type == cJSON_Number &&
      lon->type == cJSON_Number && valid->valueint)
    ego_position_age.start();
  item_in_data = cJSON_GetObjectItem(data, "device_id");
  if (item_in_data)
    vehicle_info_ctx.device_id = item_in_data->valuestring;
  item_in_data = cJSON_GetObjectItem(data, "vehicle_type");
  if (item_in_data)
    vehicle_info_ctx.vehicle_type = item_in_data->valueint;
  item_in_data = cJSON_GetObjectItem(data, "vip_status");
  if (item_in_data)
    vehicle_info_ctx.vip_status = item_in_data->valueint;
  item_in_data = cJSON_GetObjectItem(data, "vehicle_num");
  if (item_in_data)
    vehicle_info_ctx.vehicle_num = item_in_data->valuestring;
  item_in_data = cJSON_GetObjectItem(data, "drive_status");
  if (item_in_data)
    vehicle_info_ctx.drive_status = item_in_data->valueint;
  item_in_data = cJSON_GetObjectItem(data, "lon");
  if (item_in_data)
    vehicle_info_ctx.lon = item_in_data->valuedouble;
  item_in_data = cJSON_GetObjectItem(data, "lat");
  if (item_in_data)
    vehicle_info_ctx.lat = item_in_data->valuedouble;
  item_in_data = cJSON_GetObjectItem(data, "ele");
  if (item_in_data)
    vehicle_info_ctx.ele = item_in_data->valuedouble;
  item_in_data = cJSON_GetObjectItem(data, "hea");
  if (item_in_data)
    vehicle_info_ctx.hea = item_in_data->valuedouble;
  item_in_data = cJSON_GetObjectItem(data, "spd");
  if (item_in_data)
    vehicle_info_ctx.spd = item_in_data->valuedouble;
  item_in_data = cJSON_GetObjectItem(data, "pos_valid");
  if (item_in_data)
    vehicle_info_ctx.pos_valid = item_in_data->valueint;
  item_in_data = cJSON_GetObjectItem(data, "emergencyStatus");
  if (item_in_data)
    vehicle_info_ctx.emergencyStatus = item_in_data->valueint;
  item_in_data = cJSON_GetObjectItem(data, "absActivate");
  if (item_in_data)
    vehicle_info_ctx.absActivate = item_in_data->valueint;
  item_in_data = cJSON_GetObjectItem(data, "outofControl");
  if (item_in_data)
    vehicle_info_ctx.outofControl = item_in_data->valueint;
  item_in_data = cJSON_GetObjectItem(data, "gnsStatus");
  if (item_in_data)
    vehicle_info_ctx.gnsStatus = item_in_data->valueint;
  item_in_data = cJSON_GetObjectItem(data, "V2xCover");
  if (item_in_data)
    vehicle_info_ctx.V2xCover = item_in_data->valueint;
  item_in_data = cJSON_GetObjectItem(data, "current_time");
  if (item_in_data)
    vehicle_info_ctx.current_time = item_in_data->valueint;

  emit parse_vehicle_info_ok(vehicle_info_ctx);
}

void vu4004_v2x::parse_other_vehicle_info(cJSON *root) {
  cJSON *data, *other_vehicle_info, *item_in_other_vehicle_info;
  int idx, other_vehicle_num;
  data = cJSON_GetObjectItem(root, "data"); // data是个数组
  other_vehicle_num = cJSON_GetArraySize(data);
  other_position_age.invalidate();
  memset(other_vehicle_info_ctx, 0, sizeof(other_vehicle_info_ctx));
  const cJSON *first = cJSON_GetArrayItem(data, 0);
  const cJSON *lat = cJSON_GetObjectItem(first, "lat");
  const cJSON *lon = cJSON_GetObjectItem(first, "lon");
  if (lat && lon && lat->type == cJSON_Number && lon->type == cJSON_Number)
    other_position_age.start();

  for (idx = 0; idx < other_vehicle_num && idx < MAX_OTHER_VEH; idx++) {
    other_vehicle_info = cJSON_GetArrayItem(data, idx);

    item_in_other_vehicle_info =
        cJSON_GetObjectItem(other_vehicle_info, "flag");
    if (item_in_other_vehicle_info)
      other_vehicle_info_ctx[idx].flag = item_in_other_vehicle_info->valueint;
    item_in_other_vehicle_info =
        cJSON_GetObjectItem(other_vehicle_info, "device_id");
    if (item_in_other_vehicle_info)
      other_vehicle_info_ctx[idx].device_id =
          item_in_other_vehicle_info->valuestring;
    item_in_other_vehicle_info =
        cJSON_GetObjectItem(other_vehicle_info, "vehicle_type");
    if (item_in_other_vehicle_info)
      other_vehicle_info_ctx[idx].vehicle_type =
          item_in_other_vehicle_info->valueint;
    item_in_other_vehicle_info =
        cJSON_GetObjectItem(other_vehicle_info, "vehicle_num");
    if (item_in_other_vehicle_info)
      other_vehicle_info_ctx[idx].vehicle_num =
          item_in_other_vehicle_info->valuestring;
    item_in_other_vehicle_info =
        cJSON_GetObjectItem(other_vehicle_info, "security");
    if (item_in_other_vehicle_info)
      other_vehicle_info_ctx[idx].security =
          item_in_other_vehicle_info->valueint;
    item_in_other_vehicle_info = cJSON_GetObjectItem(other_vehicle_info, "lon");
    if (item_in_other_vehicle_info)
      other_vehicle_info_ctx[idx].lon = item_in_other_vehicle_info->valuedouble;
    item_in_other_vehicle_info = cJSON_GetObjectItem(other_vehicle_info, "lat");
    if (item_in_other_vehicle_info)
      other_vehicle_info_ctx[idx].lat = item_in_other_vehicle_info->valuedouble;
    item_in_other_vehicle_info = cJSON_GetObjectItem(other_vehicle_info, "ele");
    if (item_in_other_vehicle_info)
      other_vehicle_info_ctx[idx].ele = item_in_other_vehicle_info->valuedouble;
    item_in_other_vehicle_info = cJSON_GetObjectItem(other_vehicle_info, "hea");
    if (item_in_other_vehicle_info)
      other_vehicle_info_ctx[idx].hea = item_in_other_vehicle_info->valuedouble;
    item_in_other_vehicle_info = cJSON_GetObjectItem(other_vehicle_info, "spd");
    if (item_in_other_vehicle_info)
      other_vehicle_info_ctx[idx].spd = item_in_other_vehicle_info->valuedouble;

    emit parse_other_vehicle_info_ok();
  }
}

void vu4004_v2x::parse_rsi_data(cJSON *root) {
  cJSON *data, *path_list, *item_in_path_list, *path_points,
      *item_in_path_points; // path_list path_points都是数组

  data = cJSON_GetObjectItem(root, "data");
  path_list = cJSON_GetObjectItem(data, "path_list");
  item_in_path_list = cJSON_GetArrayItem(path_list, 0);
  path_points = cJSON_GetObjectItem(item_in_path_list, "path_points");

  item_in_path_points = cJSON_GetArrayItem(path_points, 1);
  rsi_data.traffic_light_phase =
      cJSON_GetObjectItem(item_in_path_points, "ele")->valuedouble;

  item_in_path_points = cJSON_GetArrayItem(path_points, 2);
  rsi_data.traffic_light_sec =
      cJSON_GetObjectItem(item_in_path_points, "ele")->valuedouble;

  item_in_path_points = cJSON_GetArrayItem(path_points, 3);
  rsi_data.event_type =
      cJSON_GetObjectItem(item_in_path_points, "ele")->valuedouble;

  item_in_path_points = cJSON_GetArrayItem(path_points, 4);
  rsi_data.event_radius =
      cJSON_GetObjectItem(item_in_path_points, "ele")->valuedouble;

  item_in_path_points = cJSON_GetArrayItem(path_points, 5);
  rsi_data.event_lat =
      cJSON_GetObjectItem(item_in_path_points, "ele")->valuedouble;

  item_in_path_points = cJSON_GetArrayItem(path_points, 6);
  rsi_data.event_lon =
      cJSON_GetObjectItem(item_in_path_points, "ele")->valuedouble;

  item_in_path_points = cJSON_GetArrayItem(path_points, 7);
  rsi_data.spd_limit_value =
      cJSON_GetObjectItem(item_in_path_points, "ele")->valuedouble;

  item_in_path_points = cJSON_GetArrayItem(path_points, 8);
  rsi_data.abnormal_veh =
      cJSON_GetObjectItem(item_in_path_points, "ele")->valuedouble;

  item_in_path_points = cJSON_GetArrayItem(path_points, 9);
  rsi_data.out_of_ctrl_veh =
      cJSON_GetObjectItem(item_in_path_points, "ele")->valuedouble;

  item_in_path_points = cJSON_GetArrayItem(path_points, 10);
  rsi_data.traffic_participants =
      cJSON_GetObjectItem(item_in_path_points, "ele")->valuedouble;

  emit parse_rsi_data_ok(rsi_data);
}

double vu4004_v2x::llh2dist(double lat, double lon, double lat_origin,
                            double lon_origin) {
  double m_PLato = lat_origin * M_PI / 180.0;
  double m_PLo = lon_origin * M_PI / 180.0;

  double m_lat = lat * M_PI / 180.0;
  double m_lon = lon * M_PI / 180.0;

  double m_x, m_y;

  double PS;  //
  double PSo; //
  double PDL; //
  double Pt;  //
  double PN;  //
  double PW;  //

  double PB1, PB2, PB3, PB4, PB5, PB6, PB7, PB8, PB9;
  double PA, PB, PC, PD, PE, PF, PG, PH, PI;
  double Pe;  //
  double Pet; //
  double Pnn; //
  double AW, FW, Pmo;

  Pmo = 0.9999;

  /*WGS84 Parameters*/
  AW = 6378137.0;           // Semimajor Axis
  FW = 1.0 / 298.257222101; // 298.257223563 //Geometrical flattening

  Pe = static_cast<double>(std::sqrt(2.0 * FW - std::pow(FW, 2)));
  Pet =
      static_cast<double>(std::sqrt(std::pow(Pe, 2) / (1.0 - std::pow(Pe, 2))));

  PA = static_cast<double>(
      1.0 + 3.0 / 4.0 * std::pow(Pe, 2) + 45.0 / 64.0 * std::pow(Pe, 4) +
      175.0 / 256.0 * std::pow(Pe, 6) + 11025.0 / 16384.0 * std::pow(Pe, 8) +
      43659.0 / 65536.0 * std::pow(Pe, 10) +
      693693.0 / 1048576.0 * std::pow(Pe, 12) +
      19324305.0 / 29360128.0 * std::pow(Pe, 14) +
      4927697775.0 / 7516192768.0 * std::pow(Pe, 16));

  PB = static_cast<double>(
      3.0 / 4.0 * std::pow(Pe, 2) + 15.0 / 16.0 * std::pow(Pe, 4) +
      525.0 / 512.0 * std::pow(Pe, 6) + 2205.0 / 2048.0 * std::pow(Pe, 8) +
      72765.0 / 65536.0 * std::pow(Pe, 10) +
      297297.0 / 262144.0 * std::pow(Pe, 12) +
      135270135.0 / 117440512.0 * std::pow(Pe, 14) +
      547521975.0 / 469762048.0 * std::pow(Pe, 16));

  PC = static_cast<double>(
      15.0 / 64.0 * std::pow(Pe, 4) + 105.0 / 256.0 * std::pow(Pe, 6) +
      2205.0 / 4096.0 * std::pow(Pe, 8) + 10395.0 / 16384.0 * std::pow(Pe, 10) +
      1486485.0 / 2097152.0 * std::pow(Pe, 12) +
      45090045.0 / 58720256.0 * std::pow(Pe, 14) +
      766530765.0 / 939524096.0 * std::pow(Pe, 16));

  PD = static_cast<double>(35.0 / 512.0 * std::pow(Pe, 6) +
                           315.0 / 2048.0 * std::pow(Pe, 8) +
                           31185.0 / 131072.0 * std::pow(Pe, 10) +
                           165165.0 / 524288.0 * std::pow(Pe, 12) +
                           45090045.0 / 117440512.0 * std::pow(Pe, 14) +
                           209053845.0 / 469762048.0 * std::pow(Pe, 16));

  PE = static_cast<double>(315.0 / 16384.0 * std::pow(Pe, 8) +
                           3465.0 / 65536.0 * std::pow(Pe, 10) +
                           99099.0 / 1048576.0 * std::pow(Pe, 12) +
                           4099095.0 / 29360128.0 * std::pow(Pe, 14) +
                           348423075.0 / 1879048192.0 * std::pow(Pe, 16));

  PF = static_cast<double>(693.0 / 131072.0 * std::pow(Pe, 10) +
                           9009.0 / 524288.0 * std::pow(Pe, 12) +
                           4099095.0 / 117440512.0 * std::pow(Pe, 14) +
                           26801775.0 / 469762048.0 * std::pow(Pe, 16));

  PG = static_cast<double>(3003.0 / 2097152.0 * std::pow(Pe, 12) +
                           315315.0 / 58720256.0 * std::pow(Pe, 14) +
                           11486475.0 / 939524096.0 * std::pow(Pe, 16));

  PH = static_cast<double>(45045.0 / 117440512.0 * std::pow(Pe, 14) +
                           765765.0 / 469762048.0 * std::pow(Pe, 16));

  PI = static_cast<double>(765765.0 / 7516192768.0 * std::pow(Pe, 16));

  PB1 = static_cast<double>(AW) * (1.0 - std::pow(Pe, 2)) * PA;
  PB2 = static_cast<double>(AW) * (1.0 - std::pow(Pe, 2)) * PB / -2.0;
  PB3 = static_cast<double>(AW) * (1.0 - std::pow(Pe, 2)) * PC / 4.0;
  PB4 = static_cast<double>(AW) * (1.0 - std::pow(Pe, 2)) * PD / -6.0;
  PB5 = static_cast<double>(AW) * (1.0 - std::pow(Pe, 2)) * PE / 8.0;
  PB6 = static_cast<double>(AW) * (1.0 - std::pow(Pe, 2)) * PF / -10.0;
  PB7 = static_cast<double>(AW) * (1.0 - std::pow(Pe, 2)) * PG / 12.0;
  PB8 = static_cast<double>(AW) * (1.0 - std::pow(Pe, 2)) * PH / -14.0;
  PB9 = static_cast<double>(AW) * (1.0 - std::pow(Pe, 2)) * PI / 16.0;

  PS = static_cast<double>(PB1) * m_lat + PB2 * std::sin(2.0 * m_lat) +
       PB3 * std::sin(4.0 * m_lat) + PB4 * std::sin(6.0 * m_lat) +
       PB5 * std::sin(8.0 * m_lat) + PB6 * std::sin(10.0 * m_lat) +
       PB7 * std::sin(12.0 * m_lat) + PB8 * std::sin(14.0 * m_lat) +
       PB9 * std::sin(16.0 * m_lat);

  PSo = static_cast<double>(PB1) * m_PLato + PB2 * std::sin(2.0 * m_PLato) +
        PB3 * std::sin(4.0 * m_PLato) + PB4 * std::sin(6.0 * m_PLato) +
        PB5 * std::sin(8.0 * m_PLato) + PB6 * std::sin(10.0 * m_PLato) +
        PB7 * std::sin(12.0 * m_PLato) + PB8 * std::sin(14.0 * m_PLato) +
        PB9 * std::sin(16.0 * m_PLato);

  PDL = static_cast<double>(m_lon) - m_PLo;
  Pt = static_cast<double>(std::tan(m_lat));
  PW = static_cast<double>(
      std::sqrt(1.0 - std::pow(Pe, 2) * std::pow(std::sin(m_lat), 2)));
  PN = static_cast<double>(AW) / PW;
  Pnn = static_cast<double>(
      std::sqrt(std::pow(Pet, 2) * std::pow(std::cos(m_lat), 2)));

  m_x = static_cast<double>(
      ((PS - PSo) +
       (1.0 / 2.0) * PN * std::pow(std::cos(m_lat), 2.0) * Pt *
           std::pow(PDL, 2.0) +
       (1.0 / 24.0) * PN * std::pow(std::cos(m_lat), 4) * Pt *
           (5.0 - std::pow(Pt, 2) + 9.0 * std::pow(Pnn, 2) +
            4.0 * std::pow(Pnn, 4)) *
           std::pow(PDL, 4) -
       (1.0 / 720.0) * PN * std::pow(std::cos(m_lat), 6) * Pt *
           (-61.0 + 58.0 * std::pow(Pt, 2) - std::pow(Pt, 4) -
            270.0 * std::pow(Pnn, 2) +
            330.0 * std::pow(Pt, 2) * std::pow(Pnn, 2)) *
           std::pow(PDL, 6) -
       (1.0 / 40320.0) * PN * std::pow(std::cos(m_lat), 8) * Pt *
           (-1385.0 + 3111 * std::pow(Pt, 2) - 543 * std::pow(Pt, 4) +
            std::pow(Pt, 6)) *
           std::pow(PDL, 8)) *
      Pmo);

  m_y = static_cast<double>(
      (PN * std::cos(m_lat) * PDL -
       1.0 / 6.0 * PN * std::pow(std::cos(m_lat), 3) *
           (-1 + std::pow(Pt, 2) - std::pow(Pnn, 2)) * std::pow(PDL, 3) -
       1.0 / 120.0 * PN * std::pow(std::cos(m_lat), 5) *
           (-5.0 + 18.0 * std::pow(Pt, 2) - std::pow(Pt, 4) -
            14.0 * std::pow(Pnn, 2) +
            58.0 * std::pow(Pt, 2) * std::pow(Pnn, 2)) *
           std::pow(PDL, 5) -
       1.0 / 5040.0 * PN * std::pow(std::cos(m_lat), 7) *
           (-61.0 + 479.0 * std::pow(Pt, 2) - 179.0 * std::pow(Pt, 4) +
            std::pow(Pt, 6)) *
           std::pow(PDL, 7)) *
      Pmo);

  return m_x * m_x + m_y * m_y;
}

/* double llh2d(double lat, double lon, double lat2, double lon2) {
  double rad_lat = lat * M_PI / 180.0;
  double rad_lon = lon * M_PI / 180.0;
  double rad_lat2 = lat2 * M_PI / 180.0;
  double rad_lon2 = lon2 * M_PI / 180.0;

  double a = rad_lat - rad_lat2;
  double b = rad_lon - rad_lon2;

  return 2 *
         asin(sqrt(sin(a / 2) * sin(a / 2) +
                   cos(rad_lat) * cos(rad_lat2) * sin(b / 2) * sin(b / 2))) *

         6378.137;
} */

bool vu4004_v2x::positions_valid() const {
  const auto valid_coordinate = [](double lat, double lon) {
    return std::isfinite(lat) && std::isfinite(lon) &&
           std::abs(lat) <= 90 && std::abs(lon) <= 180 &&
           !(lat == 0 && lon == 0);
  };
  return ego_position_age.isValid() && other_position_age.isValid() &&
         ego_position_age.elapsed() <= 2000 && other_position_age.elapsed() <= 2000 &&
         vehicle_info_ctx.pos_valid &&
         valid_coordinate(vehicle_info_ctx.lat, vehicle_info_ctx.lon) &&
         valid_coordinate(other_vehicle_info_ctx[0].lat, other_vehicle_info_ctx[0].lon);
}

void vu4004_v2x::process() {
  struct sockaddr_in tmp_addr;
  socklen_t tmp_addr_len = sizeof(tmp_addr);
  char str[JSON_STR_MAX_SIZE];
  cJSON *root, *tag;
  QElapsedTimer timer;
  cv::Mat rgb_frame;
  QImage qt_img;
  int ret;

  send_register_req();

  // 开始计时
  timer.start();

  QElapsedTimer brake_timer;
  brake_timer.start();
  double lat[11] = {}, lon[11] = {};
  int idx = 0;
  bool previous_fcw = false, previous_fbw = false;
  emit fcw_trigger(false);
  emit fbw_trigger(false);

  while (running) {
    // 1分钟发送一次心跳信息
    if (timer.elapsed() > 60000) {
      active_req_ctx.unique = register_respond_ctx.unique;
      send_active_req();
      timer.restart();
    }

    // 从OBU更新数据
    memset(str, 0, sizeof(str));
    ret = recvfrom(sockfd, str, sizeof(str), 0, (struct sockaddr *)&tmp_addr,
                   (socklen_t *)&tmp_addr_len);
    if (ret > 0) {
      root = cJSON_Parse(str);
      tag = cJSON_GetObjectItem(root, "tag");
      if (!tag || tag->type != cJSON_Number) {
        cJSON_Delete(root);
        usleep(10000);
        continue;
      }
      switch (tag->valueint) {
      case 2002:
        parse_register_respond(root);
        break;
      case 2101:
        parse_vehicle_info(root);
        break;
      case 2102:
        parse_other_vehicle_info(root);
        break;
      case 2108:
        parse_rsi_data(root);
        break;
      }
      // cJSON_Delete(root);
    }

    cv::Mat frame;
    try {
      if (cam_capture->isOpened()) cam_capture->read(frame);
    } catch (const cv::Exception &error) {
      qWarning() << "V2X camera unavailable:" << error.what();
      frame.release();
    }

    if (!frame.empty()) {
    // 绿灯时车速引导
    if (rsi_data.traffic_light_phase == 3) {
      std::string str;
      double d = llh2dist(vehicle_info_ctx.lat, vehicle_info_ctx.lon,
                          TRAFFIC_LIGHT_LAT, TRAFFIC_LIGHT_LON);

      str = "distance to traffic light: " + std::to_string(d) + "m";
      cv::putText(frame, str, cv::Point(10, 40), cv::FONT_HERSHEY_SIMPLEX, 1,
                  cv::Scalar(60, 213, 67), 4, cv::LINE_AA);
      str = "recommended speed: " +
            std::to_string(d / rsi_data.traffic_light_sec * 3.6) + "km/h";
      cv::putText(frame, str, cv::Point(10, 80), cv::FONT_HERSHEY_SIMPLEX, 1,
                  cv::Scalar(60, 213, 67), 4, cv::LINE_AA);
      int val = (timer.elapsed() / 10) % 255;
      cv::arrowedLine(frame, cv::Point(200, 180), cv::Point(200, 120),
                      cv::Scalar(0, val, 0), 4, cv::LINE_AA);
      cv::arrowedLine(frame, cv::Point(240, 180), cv::Point(240, 120),
                      cv::Scalar(0, val, 0), 4, cv::LINE_AA);
      cv::arrowedLine(frame, cv::Point(280, 180), cv::Point(280, 120),
                      cv::Scalar(0, val, 0), 4, cv::LINE_AA);
    }

    // 红灯时闯红灯预警
    if (rsi_data.traffic_light_phase == 1) {
      std::string str;
      double d = llh2dist(vehicle_info_ctx.lat, vehicle_info_ctx.lon,
                          TRAFFIC_LIGHT_LAT, TRAFFIC_LIGHT_LON);

      str =
          "red light countdown: " + std::to_string(rsi_data.traffic_light_sec) +
          "s";
      cv::putText(frame, str, cv::Point(10, 40), cv::FONT_HERSHEY_SIMPLEX, 1,
                  cv::Scalar(0, 0, 255), 4, cv::LINE_AA);
      str = "keep speed below " +
            std::to_string(d / rsi_data.traffic_light_sec * 3.6) + " km/h";
      cv::putText(frame, str, cv::Point(10, 80), cv::FONT_HERSHEY_SIMPLEX, 1,
                  cv::Scalar(0, 0, 255), 4, cv::LINE_AA);
    }

    cv::cvtColor(frame, rgb_frame, cv::COLOR_BGR2RGB);
    qt_img = QImage((const unsigned char *)rgb_frame.data, rgb_frame.cols,
                    rgb_frame.rows, rgb_frame.step, QImage::Format_RGB888);
    emit cam_frame_ok(qt_img);
    }

    // Missing or stale OBU positions are unknown, not a zero-metre gap.
    const bool valid = positions_valid();
    const bool fcw = valid && llh2dist(vehicle_info_ctx.lat, vehicle_info_ctx.lon,
                                      other_vehicle_info_ctx[0].lat,
                                      other_vehicle_info_ctx[0].lon) < 6;
    if (fcw != previous_fcw) {
      emit fcw_trigger(fcw);
      previous_fcw = fcw;
    }
    // Keep position reads and writes on this worker, avoiding the old data race.
    bool fbw = previous_fbw;
    if (!valid) {
      idx = 0;
      fbw = false;
    } else if (brake_timer.elapsed() >= 100) {
      brake_timer.restart();
      lat[idx] = other_vehicle_info_ctx[0].lat;
      lon[idx++] = other_vehicle_info_ctx[0].lon;
      if (idx == 11) {
        double diff[10] = {};
        fbw = false;
        for (int i = 0; i < 10; ++i)
          diff[i] = llh2dist(lat[i + 1], lon[i + 1], lat[0], lon[0]);
        for (int i = 0; i < 9; ++i)
          fbw = fbw || (diff[i + 1] - diff[i] < -1);
        idx = 0;
      }
    }
    if (fbw != previous_fbw) {
      emit fbw_trigger(fbw);
      previous_fbw = fbw;
    }
    usleep(50000);
  }
}
