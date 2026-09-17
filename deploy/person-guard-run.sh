#!/bin/bash
set -e
root=/home/nvidia/Desktop/bigcar-console
if [ -f "$root/runtime/person-camera.env" ]; then
  source "$root/runtime/person-camera.env"
fi
if [ "${2:-}" = stop ]; then
  case "$1" in
    camera) node=/person_camera/camera ;;
    observer) node=/person_camera_observer ;;
    gate) node=/person_command_gate ;;
    detector) exit 0 ;;
    *) exit 2 ;;
  esac
  exec docker exec autoware_ai_orin bash -c 'source /opt/ros/melodic/setup.bash; rosnode kill "$1"' bash "$node"
fi
case "$1" in
  detector)
    exec /usr/bin/python3 "$root/backend/person_detector_server.py" \
      --prototxt "$root/runtime/deploy.prototxt" \
      --weights "$root/runtime/mobilenet_iter_73000.caffemodel"
    ;;
  camera)
    exec docker exec autoware_ai_orin bash -c '
      source /opt/ros/melodic/setup.bash
      source /root/person_camera_ws/devel/setup.bash
      export LD_LIBRARY_PATH=/root/person_camera_deps/install/lib:$LD_LIBRARY_PATH
      exec roslaunch astra_camera astra_pro_plus.launch camera_name:=person_camera depth_align:=true enable_ir:=false enable_point_cloud:=false color_depth_synchronization:=false'
    ;;
  observer)
    exec docker exec autoware_ai_orin bash -c '
      source /opt/ros/melodic/setup.bash
      exec python2 /from_host/bigcar-console/backend/person_camera_observer.py "_camera_to_front_m:=$1" "_alignment_verified:=$2"' bash "${PERSON_CAMERA_TO_FRONT_M:-0.0}" "${PERSON_ALIGNMENT_VERIFIED:-false}"
    ;;
  gate)
    exec docker exec autoware_ai_orin bash -c '
      source /opt/ros/melodic/setup.bash
      source /root/autoware_1.14.0/install/setup.bash
      exec python2 /from_host/bigcar-console/backend/person_command_gate.py _input:=/person_guard/ctrl_cmd_input _output:=/ctrl_cmd _status:=/person_guard/status _require_arm:=true'
    ;;
  *) exit 2 ;;
esac
