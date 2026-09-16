#include "f40_radar.h"
#include <QString>
#include <net/if.h>
#include <sys/ioctl.h>
#include <unistd.h>

f40_radar::f40_radar() {
  int ret;

  // 初始化CAN
  sockfd = socket(PF_CAN, SOCK_RAW, CAN_RAW);
  if (sockfd < 0) {
    perror("socket failed");
    return;
  }

  can_name = "can1";
  struct ifreq ifr;
  strcpy(ifr.ifr_name, can_name.c_str());
  ioctl(sockfd, SIOCGIFINDEX, &ifr);

  struct sockaddr_can addr;
  memset(&addr, 0, sizeof(addr));
  addr.can_family = AF_CAN;
  addr.can_ifindex = ifr.ifr_ifindex;
  ret = bind(sockfd, (struct sockaddr *)(&addr), sizeof(addr));
  if (ret < 0) {
    perror("can bind error");
    return;
  }

  // 实训室低速运行：预留 50 mm 净安全距离。
  // 下方判定还会叠加各雷达的安装偏置（100/140/180 mm）。
  set_radar(0xb5, 50);

  // 接收线程
  recv_data_thd = new std::thread(&f40_radar::recv_data, this);
}

f40_radar::~f40_radar() {
  delete recv_data_thd;
  close(sockfd);
}

void f40_radar::recv_data() {
  char dist_str[5];
  QString warn_str;

  while (true) {
    read(sockfd, &recv_frame, sizeof(recv_frame));

    switch (recv_frame.can_id) {
    case 0x611:
      snprintf(dist_str, 5, "%02x%02x", recv_frame.data[0], recv_frame.data[1]);
      dist[0] = QString(dist_str).toInt();
      snprintf(dist_str, 5, "%02x%02x", recv_frame.data[2], recv_frame.data[3]);
      dist[1] = QString(dist_str).toInt();
      snprintf(dist_str, 5, "%02x%02x", recv_frame.data[4], recv_frame.data[5]);
      dist[2] = QString(dist_str).toInt();
      snprintf(dist_str, 5, "%02x%02x", recv_frame.data[6], recv_frame.data[7]);
      dist[3] = QString(dist_str).toInt();
      break;

    case 0x612:
      snprintf(dist_str, 5, "%02x%02x", recv_frame.data[0], recv_frame.data[1]);
      dist[4] = QString(dist_str).toInt();
      snprintf(dist_str, 5, "%02x%02x", recv_frame.data[2], recv_frame.data[3]);
      dist[5] = QString(dist_str).toInt();
      snprintf(dist_str, 5, "%02x%02x", recv_frame.data[4], recv_frame.data[5]);
      dist[6] = QString(dist_str).toInt();
      snprintf(dist_str, 5, "%02x%02x", recv_frame.data[6], recv_frame.data[7]);
      dist[7] = QString(dist_str).toInt();
      break;

    default:
      break;
    }

    // for (int i = 0; i < 8; i++) {
    //   printf("%d    ", dist[i]);
    // }
    // printf("\n");

    // 车头左前超声波雷达为第一，顺时针增加序号
    // 第5 6个毫米波雷达的安装有遮挡，弃之不用
    if ((dist[0] < warn_thold + 100) || (dist[1] < warn_thold + 100)) {
      warn_str = "即将撞上前方障碍物，车辆急停生效";
      goto cw_triggered;
    }
    if ((dist[2] < warn_thold + 140) || (dist[3] < warn_thold + 140)) {
      warn_str = "即将撞上右侧障碍物，车辆急停生效";
      goto cw_triggered;
    }
    if ((dist[6] < warn_thold + 180) || (dist[7] < warn_thold + 180)) {
      warn_str = "即将撞上左侧障碍物，车辆急停生效";
      goto cw_triggered;
    }

    emit cw_trigger(false, warn_str);
    continue;

  cw_triggered:
    emit cw_trigger(true, warn_str);
  }
}

void f40_radar::set_radar(uint8_t set_mode, uint16_t set_warn_thold) {
  int ret;

  warn_thold = set_warn_thold;

  send_frame.can_id = 0x601;
  send_frame.can_dlc = 3;
  uint8_t cmd[3] = {set_mode, 0x10, 0xff};
  memcpy(send_frame.data, cmd, sizeof(cmd));
  ret = write(sockfd, &send_frame, sizeof(send_frame));
  if (ret < 0) {
    perror("set f40 mode error");
  }
}
