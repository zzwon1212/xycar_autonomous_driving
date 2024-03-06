#!/usr/bin/env python3
#
# Copyright 1993-2019 NVIDIA Corporation.  All rights reserved.
#
# NOTICE TO LICENSEE:
#
# This source code and/or documentation ("Licensed Deliverables") are
# subject to NVIDIA intellectual property rights under U.S. and
# international Copyright laws.
#
# These Licensed Deliverables contained herein is PROPRIETARY and
# CONFIDENTIAL to NVIDIA and is being provided under the terms and
# conditions of a form of NVIDIA software license agreement by and
# between NVIDIA and Licensee ("License Agreement") or electronically
# accepted by Licensee.  Notwithstanding any terms or conditions to
# the contrary in the License Agreement, reproduction or disclosure
# of the Licensed Deliverables to any third party without the express
# written consent of NVIDIA is prohibited.
#
# NOTWITHSTANDING ANY TERMS OR CONDITIONS TO THE CONTRARY IN THE
# LICENSE AGREEMENT, NVIDIA MAKES NO REPRESENTATION ABOUT THE
# SUITABILITY OF THESE LICENSED DELIVERABLES FOR ANY PURPOSE.  IT IS
# PROVIDED "AS IS" WITHOUT EXPRESS OR IMPLIED WARRANTY OF ANY KIND.
# NVIDIA DISCLAIMS ALL WARRANTIES WITH REGARD TO THESE LICENSED
# DELIVERABLES, INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY,
# NONINFRINGEMENT, AND FITNESS FOR A PARTICULAR PURPOSE.
# NOTWITHSTANDING ANY TERMS OR CONDITIONS TO THE CONTRARY IN THE
# LICENSE AGREEMENT, IN NO EVENT SHALL NVIDIA BE LIABLE FOR ANY
# SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL DAMAGES, OR ANY
# DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS,
# WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS
# ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE
# OF THESE LICENSED DELIVERABLES.
#
# U.S. Government End Users.  These Licensed Deliverables are a
# "commercial item" as that term is defined at 48 C.F.R. 2.101 (OCT
# 1995), consisting of "commercial computer software" and "commercial
# computer software documentation" as such terms are used in 48
# C.F.R. 12.212 (SEPT 1995) and is provided to the U.S. Government
# only as a commercial end item.  Consistent with 48 C.F.R.12.212 and
# 48 C.F.R. 227.7202-1 through 227.7202-4 (JUNE 1995), all
# U.S. Government End Users acquire the Licensed Deliverables with
# only those rights set forth herein.
#
# Any use of the Licensed Deliverables in individual and commercial
# software must include, in the user documentation and internal
# comments to the code, the above Disclaimer and U.S. Government End
# Users Notice.
#

import sys, os
import time
import numpy as np
import math
import cv2
# from cv_bridge import CvBridge  # If you use Python2
from PIL import Image, ImageDraw

import rospy
from std_msgs.msg import String
from sensor_msgs.msg import Image as Imageros
from yolov3_trt_ros.msg import BoundingBox, BoundingBoxes

from data_processing import PreprocessYOLO, PostprocessYOLO, ALL_CATEGORIES

import torch
if torch.cuda.is_available():
    is_cuda = True
    import tensorrt as trt
    import common
    TRT_LOGGER = trt.Logger(trt.Logger.WARNING)
    # TODO: Change this to relative path
    MODEL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "model/model_epoch4400_pretrained_04_001.trt")
else:
    is_cuda = False
    from with_cpu.yolov3 import DarkNet53
    from with_cpu.util.tools import *
    MODEL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "model/model_epoch4400_pretrained.pth")

CFG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "config/yolov3-tiny_tstl_416.cfg")
NUM_CLASS = 8
# INPUT_IMG = '/home/nvidia/xycar_ws/src/yolov3_trt_ros/src/video1_2.png'

xycar_image = np.empty(shape=[0])

# global CAMERA_MATRIX, DISTORT_COEFF, HOMOGRAPHY
CAMERA_MATRIX = np.array([[352.494189, 0.000000, 295.823760],
                          [0.000000, 353.504572, 239.649689],
                          [0.000000, 0.000000, 1.000000]])
DISTORT_COEFF = np.array([-0.318744, 0.088199, 0.000167, 0.000699, 0.000000])
HOMOGRAPHY = [[-1.91653414e-01, -1.35359667e+00, 3.09165939e+02],
              [8.42075718e-04, -2.87835672e+00, 6.16140688e+02],
              [9.75538785e-06, -5.04169177e-03, 1.00000000e+00]]

class yolov3_trt(object):
    def __init__(self):
        self.cfg_file_path = CFG
        self.num_class = NUM_CLASS
        width, height, masks, anchors = parse_cfg_wh(self.cfg_file_path)
        self.model_path = MODEL
        self.show_img = False
        # Two-dimensional tuple with the target network's (spatial) input resolution in HW ordered
        input_resolution_yolov3_WH = (width, height)
        # Create a pre-processor object by specifying the required input resolution for YOLOv3
        self.preprocessor = PreprocessYOLO(input_resolution_yolov3_WH)

        # Output shapes expected by the post-processor
        output_channels = (self.num_class + 5) * 3
        if len(masks) == 2:
            self.output_shapes = [(1, output_channels, height//32, width//32), (1, output_channels, height//16, width//16)]
        else:
            self.output_shapes = [(1, output_channels, height//32, width//32), (1, output_channels, height//16, width//16), (1, output_channels, height//8, width//8)]

        postprocessor_args = {"yolo_masks": masks,                    # A list of 3 three-dimensional tuples for the YOLO masks
                              "yolo_anchors": anchors,
                              "obj_threshold": 0.5,                                               # Threshold for object coverage, float value between 0 and 1
                              "nms_threshold": 0.3,                                               # Threshold for non-max suppression algorithm, float value between 0 and 1
                              "yolo_input_resolution": input_resolution_yolov3_WH,
                              "num_class": self.num_class}

        self.postprocessor = PostprocessYOLO(**postprocessor_args)

        if is_cuda:
            self.engine = get_engine(self.model_path)
            self.context = self.engine.create_execution_context()

        self.detection_pub = rospy.Publisher('/yolov3_trt_ros/detections', BoundingBoxes, queue_size=1)


    def detect(self):
        rate = rospy.Rate(10)
        image_sub = rospy.Subscriber("/usb_cam/image_raw", Imageros, img_callback)

        while not rospy.is_shutdown():
            rate.sleep()

            # if xycar_image is empty, skip inference
            if xycar_image.shape[0] == 0:
                continue

            # if self.show_img:
            #     show_trt = cv2.cvtColor(xycar_image, cv2.COLOR_RGB2BGR)
            #     cv2.imshow("show_trt", show_trt)
            #     cv2.waitKey(1)

            image = self.preprocessor.process(xycar_image)
            # Store the shape of the original input image in WH format, we will need it for later
            shape_orig_WH = (image.shape[3], image.shape[2])

            start_time = time.time()
            if is_cuda:  # TensorRT with GPU
                # Do inference with TensorRT
                inputs, outputs, bindings, stream = common.allocate_buffers(self.engine)

                # Set host input to the image. The common.do_inference function will copy the input to the GPU before executing.
                inputs[0].host = image
                trt_outputs = common.do_inference(self.context, bindings=bindings, inputs=inputs, outputs=outputs, stream=stream)

                # Before doing post-processing, we need to reshape the outputs as the common.do_inference will give us flat arrays.
                trt_outputs = [output.reshape(shape) for output, shape in zip(trt_outputs, self.output_shapes)]

                # Run the post-processing algorithms on the TensorRT outputs and get the bounding box details of detected objects
                boxes, classes, scores = self.postprocessor.process(trt_outputs, shape_orig_WH)
            else:  # Torch with CPU
                with torch.no_grad():
                    cfg_data = parse_hyperparam_config(self.cfg_file_path)
                    cfg_param = get_hyperparam(cfg_data)
                    model = DarkNet53(self.cfg_file_path, cfg_param)
                    model.eval()
                    checkpoint = torch.load(self.model_path, map_location=torch.device('cpu'))
                    model.load_state_dict(checkpoint["model_state_dict"])
                    img_troch = torch.from_numpy(image)
                    output = model(img_troch)

                    best_box_list = non_max_suppression(output, conf_thres=0.4, iou_thres=0.001)
                    best_box_list = best_box_list[0].numpy().astype(np.float32)
                    # if best_box_list.numel() == 0:
                    if len(best_box_list) == 0:
                        boxes = None
                        scores = None
                        classes = None
                    else:
                        boxes = best_box_list[:, :4]
                        boxes[:, 2] -= boxes[:, 0]  # xmax -> width
                        boxes[:, 3] -= boxes[:, 1]  # ymax -> height
                        scores = best_box_list[:, 4]
                        classes = best_box_list[:, 5].astype(np.int32)
            latency = time.time() - start_time
            fps = 1 / latency

            # Draw the bounding boxes onto the original input image and save it as a PNG file
            # print(boxes, classes, scores)
            if self.show_img:
                img_show = np.array(np.transpose(image[0], (1, 2, 0)) * 255, dtype=np.uint8)
                obj_detected_img = draw_bboxes(Image.fromarray(img_show), boxes, scores, classes, ALL_CATEGORIES)
                obj_detected_img_np = np.array(obj_detected_img)
                result_img = cv2.cvtColor(obj_detected_img_np, cv2.COLOR_BGR2RGB)
                cv2.putText(result_img, "FPS:" + str(int(fps)), (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2, 1)
                cv2.imshow("result",result_img)
                cv2.waitKey(1)

            ## depth estimation
            if self.show_img:
                grid_img = np.ones((540, 540, 3), dtype=np.uint8) * 255
                for i in range(grid_img.shape[0] // 90):
                    grid_img[90 * i, :, :] = [0, 0, 0]
                    grid_img[:, 90 * i, :] = [0, 0, 0]

            xdepth_list = []
            ydepth_list = []
            if boxes is not None and not classes is None:
                for box, class_id in zip(boxes, classes):
                    xmin, ymin, width, height = box
                    obj_bottom_center = np.array([xmin + width/2, ymin + height, 1])
                    grid_point = np.dot(HOMOGRAPHY, obj_bottom_center)
                    grid_point /= grid_point[2] + 0.000001
                    grid_point = np.round(grid_point).astype(int)

                    x, y = grid_point[0], grid_point[1]
                    xdepth = abs(270 - x) / 2
                    xdepth_list.append(xdepth)
                    ydepth = (540 - y) / 2
                    ydepth_list.append(ydepth)

                    if self.show_img:
                        cv2.circle(grid_img, center=(x, y), radius=6, color=(0, 0, 255), thickness=-1)
                        cv2.putText(grid_img, text=f"{ALL_CATEGORIES[class_id]}: {ydepth}cm", org=(x + 5, y), fontFace=cv2.FONT_HERSHEY_SIMPLEX, fontScale=0.5, color=(0, 0, 0))

            if self.show_img:
                cv2.imshow("grid", grid_img)
                cv2.waitKey(1)

            # publish detected objects boxes and classes
            self.publisher(boxes, scores, classes, xdepth_list, ydepth_list)


    def _write_message(self, detection_results, boxes, scores, classes, xdepth_list, ydepth_list):
        """ populate output message with input header and bounding boxes information """
        if boxes is None:
            return None
        for box, score, category, xdepth, ydepth in zip(boxes, scores, classes, xdepth_list, ydepth_list):
            # Populate darknet message
            xmin, ymin, width, height = box
            detection_msg = BoundingBox()
            detection_msg.xmin = int(xmin)
            detection_msg.xmax = int(xmin + width)
            detection_msg.ymin = int(ymin)
            detection_msg.ymax = int(ymin + height)
            detection_msg.prob = float(score)
            detection_msg.id = int(category)
            detection_msg.xdepth = int(xdepth)
            detection_msg.ydepth = int(ydepth)
            detection_results.bbox.append(detection_msg)
        return detection_results

    def publisher(self, boxes, confs, classes, xdepth_list, ydepth_list):
        """ Publishes to detector_msgs
        Parameters:
        boxes (List(List(int))) : Bounding boxes of all objects
        confs (List(double))	: Probability scores of all objects
        classes  (List(int))	: Class ID of all classes
        """
        detection_results = BoundingBoxes()
        self._write_message(detection_results, boxes, confs, classes, xdepth_list, ydepth_list)
        self.detection_pub.publish(detection_results)


#parse width, height, masks and anchors from cfg file
def parse_cfg_wh(cfg):
    masks = []
    with open(cfg, 'r') as f:
        lines = f.readlines()
        for line in lines:
            if 'width' in line:
                w = int(line[line.find('=')+1:].replace('\n',''))
            elif 'height' in line:
                h = int(line[line.find('=')+1:].replace('\n',''))
            elif 'anchors' in line:
                anchor = line.split('=')[1].replace('\n','')
                anc = [int(a) for a in anchor.split(',')]
                anchors = [(anc[i*2], anc[i*2+1]) for i in range(len(anc) // 2)]
            elif 'mask' in line:
                mask = line.split('=')[1].replace('\n','')
                m = tuple(int(a) for a in mask.split(','))
                masks.append(m)
    return w, h, masks, anchors

def img_callback(data):
    global xycar_image

    # Python 2
    # xycar_image = CvBridge().imgmsg_to_cv2(data, "bgr8")

    # Python 3
    # xycar_image == RGB
    test_image = np.frombuffer(data.data, dtype=np.uint8).reshape(data.height, data.width, -1)
    mapx, mapy = cv2.initUndistortRectifyMap(CAMERA_MATRIX, DISTORT_COEFF, None, None, (test_image.shape[1], test_image.shape[0]), 5)
    xycar_image = cv2.remap(test_image, mapx, mapy, cv2.INTER_LINEAR)

# image_raw = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
def draw_bboxes(image_raw, bboxes, confidences, categories, all_categories, bbox_color='blue'):
    """Draw the bounding boxes on the original input image and return it.

    Keyword arguments:
    image_raw -- a raw PIL Image
    bboxes -- NumPy array containing the bounding box coordinates of N objects, with shape (N,4).
    categories -- NumPy array containing the corresponding category for each object,
    with shape (N,)
    confidences -- NumPy array containing the corresponding confidence for each object,
    with shape (N,)
    all_categories -- a list of all categories in the correct ordered (required for looking up
    the category name)
    bbox_color -- an optional string specifying the color of the bounding boxes (default: 'blue')
    """
    draw = ImageDraw.Draw(image_raw)
    if bboxes is None and confidences is None and categories is None:
        return image_raw
    for box, score, category in zip(bboxes, confidences, categories):
        x_coord, y_coord, width, height = box
        left = max(0, np.floor(x_coord + 0.5).astype(int))
        top = max(0, np.floor(y_coord + 0.5).astype(int))
        right = min(image_raw.width, np.floor(x_coord + width + 0.5).astype(int))
        bottom = min(image_raw.height, np.floor(y_coord + height + 0.5).astype(int))

        draw.rectangle(((left, top), (right, bottom)), outline=bbox_color)
        draw.text((left, top - 12), f'{all_categories[category]} {score:.2f}', fill=bbox_color)

    return image_raw

def get_engine(model_path=""):
    """Attempts to load a serialized engine if available, otherwise builds a new TensorRT engine and saves it."""
    if os.path.exists(model_path):
        # If a serialized engine exists, use it instead of building an engine.
        print("Reading engine from file {}".format(model_path))
        with open(model_path, "rb") as f, trt.Runtime(TRT_LOGGER) as runtime:
            return runtime.deserialize_cuda_engine(f.read())
    else:
        print("no trt model")
        sys.exit(1)

if __name__ == '__main__':
    yolo = yolov3_trt()
    rospy.init_node('yolov3_trt_ros', anonymous=True)
    yolo.detect()
