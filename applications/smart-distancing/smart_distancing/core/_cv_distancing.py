"""OpenCV Distancing implementation"""
import math
import logging
import itertools
import os

import cv2 as cv
import numpy as np

from smart_distancing.loggers import serialize
from smart_distancing.core._distancing import BaseDistancing
from smart_distancing.core._centroid_object_tracker import CentroidTracker

__all__ = ['CvDistancing']


class CvDistancing(BaseDistancing):
    """OpenCV implementation of Distancing"""

    # TODO(mdegans): this is a hacky way of passing the frame around. rethink.
    _cv_img_tmp = None

    def __init__(self, config):
        super().__init__(config)
        self.running_video = False
        self.logger = logging.getLogger(__class__.__name__)
        self.frame_counter = itertools.count(0)
        self.tracker = CentroidTracker(
            max_disappeared=int(self.config.get_section_dict("PostProcessor")["MaxTrackFrame"]))

        detector_name_tmp = self.config.get_section_dict("Detector")["Name"]
        if self.device == 'Jetson':
            if detector_name_tmp == 'mobilenet_ssd_v2_coco':
                from smart_distancing.detectors.jetson import MobilenetSsdDetector as Detector
            else:
                raise ValueError('Failed to initiate Detector, as ' + detector_name_tmp + \
                                 ' on device ' + self.device + ' is not supported')
        elif self.device == 'EdgeTPU':
            if detector_name_tmp == 'mobilenet_ssd_v2':
                from smart_distancing.detectors.edgetpu import MobilenetSsdDetector as Detector
            elif detector_name_tmp == 'pedestrian_ssd_mobilenet_v2':
                from smart_distancing.detectors.edgetpu import PedestrianSsdDetector as Detector
            elif detector_name_tmp == 'pedestrian_ssdlite_mobilenet_v2':
                from smart_distancing.detectors.edgetpu import PedestrianSsdLiteDetector as Detector
            else:
                raise ValueError('Failed to initiate Detector, as ' + detector_name_tmp + \
                                 ' on device ' + self.device + ' is not supported')
        elif self.device == 'x86':
            if detector_name_tmp == 'mobilenet_ssd_v2_coco_2018_03_29':
                from smart_distancing.detectors.x86 import TfDetector as Detector
            else:
                raise ValueError('Failed to initiate Detector, as ' + detector_name_tmp + \
                                 ' on device ' + self.device + ' is not supported')
        elif self.device == 'Dummy':
             Detector = None
        
        if Detector is not None: 
            self.detector = Detector(self.config)
        # set the callback on the detector process, so it's called 
        self.detector.on_frame = self._on_detections


    def _resize(self, cv_image):
        cv_image = cv.resize(cv_image, self.resolution)
        resized_image = cv.resize(cv_image, tuple(self.image_size[:2]))
        return cv.cvtColor(resized_image, cv.COLOR_BGR2RGB)

    def __process(self, cv_image):
        if self.device == 'Dummy':
            return cv_image, [], None

        # Resize input image to display resolution
        cv_image = self._resize(cv_image)

        # store the frame temporarily for display:
        self._cv_img_tmp = cv_image

        # run the Detector, which calls _on_detections when it's done
        self.detector.inference(cv_image)

    def _on_detections(self, detections):
        # TODO(mdegans): move to Detector class instead of setting it as on_frame callback on init?
        # it does what's described in the Detector interface.
        w, h = self.resolution

        for obj in detections:
            box = obj["bbox"]
            x0 = box[1]
            y0 = box[0]
            x1 = box[3]
            y1 = box[2]
            obj["centroid"] = [(x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0]
            obj["bbox"] = [x0, y0, x1, y1]
            obj["centroidReal"] = [(x0 + x1) * w / 2, (y0 + y1) * h / 2, (x1 - x0) * w, (y1 - y0) * h]
            obj["bboxReal"] = [x0 * w, y0 * h, x1 * w, y1 * h]

        objects, distancings = self.calculate_distancing(detections)
        record = {
            # TODO(mdegans): fix serialization if desired. seems a lot to log.
            'fnum': next(self.frame_counter),
            'objects': len(objects),
        }
        self.logger.debug(serialize(record))
        scored_objects = vis_util.visualization_preparation(objects, distancings, self.ui._dist_threshold)
        self.ui.update(self._cv_img_tmp, scored_objects)

    def process_video(self, video_uri):
        if os.path.isfile(video_uri):
            # if the input video_uri is a file, convert to file uri
            # gstreamer's uridecodebin demands this format
            video_uri = f'file://{os.path.abspath(video_uri)}'
        # create the uridecodebin launch string.
        # NOTE(mdegans): There are some optmizations that can go here, like only
        # decoding video streams or using a more performant platform specific
        # conversion elements, however videoconvert exists on all platforms
        gst_launch_str = f'uridecodebin uri="{video_uri}" ! video/x-raw ! videoconvert ! appsink'
        input_cap = cv.VideoCapture(gst_launch_str, cv.CAP_GSTREAMER)

        if input_cap.isOpened():
            print('opened video ', video_uri)
        else:
            print('failed to load video ', video_uri)
            return

        self.running_video = True
        while input_cap.isOpened() and self.running_video:
            _, cv_image = input_cap.read()
            if np.shape(cv_image) == ():
                continue
            self.__process(cv_image)

        input_cap.release()
        self.running_video = False

    def process_image(self, image_path):
        # Process and pass the image to ui modules
        cv_image = cv.imread(image_path)
        cv_image, objects, distancings = self.detector.inference(cv_image)
        self.ui.update(cv_image, objects, distancings)

    def calculate_distancing(self, objects_list):
        """
        this function post-process the raw boxes of object detector and calculate a distance matrix
        for detected bounding boxes.
        post processing is consist of:
        1. omitting large boxes by filtering boxes which are biger than the 1/4 of the size the image.
        2. omitting duplicated boxes by applying an auxilary non-maximum-suppression.
        3. apply a simple object tracker to make the detection more robust.

        params:
        object_list: a list of dictionaries. each dictionary has attributes of a detected object such as
        "id", "centroid" (a tuple of the normalized centroid coordinates (cx,cy,w,h) of the box) and "bbox" (a tuple
        of the normalized (xmin,ymin,xmax,ymax) coordinate of the box)

        returns:
        object_list: the post processed version of the input
        distances: a NxN ndarray which i,j element is distance between i-th and l-th bounding box

        """
        new_objects_list = self.ignore_large_boxes(objects_list)
        new_objects_list = self.non_max_suppression_fast(new_objects_list,
                                                         float(self.config.get_section_dict("PostProcessor")[
                                                                   "NMSThreshold"]))
        tracked_boxes = self.tracker.update(new_objects_list)
        new_objects_list = [tracked_boxes[i] for i in tracked_boxes.keys()]
        for i, item in enumerate(new_objects_list):
            item["id"] = item["id"].split("-")[0] + "-" + str(i)

        centroids = np.array([obj["centroid"] for obj in new_objects_list])
        distances = self.calculate_box_distances(new_objects_list)

        return new_objects_list, distances

    @staticmethod
    def ignore_large_boxes(object_list):

        """
        Filter boxes which are bigger than 1/4 of the size the image.

        Args:
            object_list (:obj:`list` of :obj:`dict`): Each dictionary has attributes of a
                detected object such as "id", "centroid" (a tuple of the normalized centroid
                coordinates (cx,cy,w,h) of the box) and "bbox" (a tuple of the normalized 
                (xmin,ymin,xmax,ymax) coordinate of the box)
        Returns:
            object_list (:obj:`list` of :obj:`dict`): input object list without large boxes
        """
        large_boxes = []
        for i in range(len(object_list)):
            if (object_list[i]["centroid"][2] * object_list[i]["centroid"][3]) > 0.25:
                large_boxes.append(i)
        updated_object_list = [j for i, j in enumerate(object_list) if i not in large_boxes]
        return updated_object_list

    @staticmethod
    def non_max_suppression_fast(object_list, overlapThresh):

        """
        Remove overlapping boxes by applying an auxilary non-maximum-suppression.
        
        # Dr. Malisiewicz et al.
        https://www.pyimagesearch.com/2015/02/16/faster-non-maximum-suppression-python/

        Args:
            object_list (:obj:`list` of :obj:`dict`):
                each key is the object uid. each value has attributes of a detected object such
                "id", "centroid" (a tuple of the normalized centroid coordinates (cx,cy,w,h) of
                the box) and "bbox" (a tuple of the normalized (xmin,ymin,xmax,ymax) coordinate
                of the box)

        overlapThresh: threshold of minimum IoU of to detect two box as duplicated.

        Returns:
            object_list (:obj:`list` of :obj:`dict`):
                without duplicated boxes
        """
        boxes = np.array([item["centroid"] for item in object_list])
        corners = np.array([item["bbox"] for item in object_list])

        # initialize the list of picked indexes
        pick = []

        # TODO(author): comment this, please
        cy = boxes[:, 1]
        cx = boxes[:, 0]
        h = boxes[:, 3]
        w = boxes[:, 2]

    	# grab the coordinates of the bounding boxes
        x1 = corners[:, 0]
        x2 = corners[:, 2]
        y1 = corners[:, 1]
        y2 = corners[:, 3]

        # compute the area of the bounding boxes and sort the bounding
	    # boxes by the bottom-right y-coordinate of the bounding box
        area = (h + 1) * (w + 1)
        idxs = np.argsort(cy + (h / 2))

        # keep looping while some indexes still remain in the indexes
	    # list
        while len(idxs) > 0:
            # grab the last index in the indexes list and add the
		    # index value to the list of picked indexes
            last = len(idxs) - 1
            i = idxs[last]
            pick.append(i)


    		# find the largest (x, y) coordinates for the start of
    		# the bounding box and the smallest (x, y) coordinates
    		# for the end of the bounding box
            xx1 = np.maximum(x1[i], x1[idxs[:last]])
            yy1 = np.maximum(y1[i], y1[idxs[:last]])
            xx2 = np.minimum(x2[i], x2[idxs[:last]])
            yy2 = np.minimum(y2[i], y2[idxs[:last]])

    		# compute the width and height of the bounding box
            w = np.maximum(0, xx2 - xx1 + 1)
            h = np.maximum(0, yy2 - yy1 + 1)

            # compute the ratio of overlap
            overlap = (w * h) / area[idxs[:last]]

            # delete all indexes from the index list that have
            idxs = np.delete(idxs, np.concatenate(([last],
                np.where(overlap > overlapThresh)[0])))

        return [j for i, j in enumerate(object_list) if i in pick]

    def calculate_distance_of_two_points_of_boxes(self, first_point, second_point):

        """
        This function calculates a distance l for two input corresponding points of two detected bounding boxes.
        it is assumed that each person is H = 170 cm tall in real scene to map the distances in the image (in pixels) to
        physical distance measures (in meters).

        params:
        first_point: (x, y, h)-tuple, where x,y is the location of a point (center or each of 4 corners of a bounding box)
        and h is the height of the bounding box.
        second_point: same tuple as first_point for the corresponding point of other box

        returns:
        l:  Estimated physical distance (in centimeters) between first_point and second_point.


        """

        # estimate corresponding points distance
        [xc1, yc1, h1] = first_point
        [xc2, yc2, h2] = second_point

        dx = xc2 - xc1
        dy = yc2 - yc1

        lx = dx * 170 * (1 / h1 + 1 / h2) / 2
        ly = dy * 170 * (1 / h1 + 1 / h2) / 2

        l = math.sqrt(lx ** 2 + ly ** 2)

        return l

    def calculate_box_distances(self, nn_out):

        """
        This function calculates a distance matrix for detected bounding boxes.
        Two methods are implemented to calculate the distances, first one estimates distance of center points of the
        boxes and second one uses minimum distance of each of 4 points of bounding boxes.

        params:
        object_list: a list of dictionaries. each dictionary has attributes of a detected object such as
        "id", "centroidReal" (a tuple of the centroid coordinates (cx,cy,w,h) of the box) and "bboxReal" (a tuple
        of the (xmin,ymin,xmax,ymax) coordinate of the box)

        returns:
        distances: a NxN ndarray which i,j element is estimated distance between i-th and j-th bounding box in real scene (cm)

        """

        distances = []
        for i in range(len(nn_out)):
            distance_row = []
            for j in range(len(nn_out)):
                if i == j:
                    l = 0
                else:
                    if (self.dist_method == 'FourCornerPointsDistance'):
                        lower_left_of_first_box = [nn_out[i]["bboxReal"][0], nn_out[i]["bboxReal"][1],
                                                   nn_out[i]["centroidReal"][3]]
                        lower_right_of_first_box = [nn_out[i]["bboxReal"][2], nn_out[i]["bboxReal"][1],
                                                    nn_out[i]["centroidReal"][3]]
                        upper_left_of_first_box = [nn_out[i]["bboxReal"][0], nn_out[i]["bboxReal"][3],
                                                   nn_out[i]["centroidReal"][3]]
                        upper_right_of_first_box = [nn_out[i]["bboxReal"][2], nn_out[i]["bboxReal"][3],
                                                    nn_out[i]["centroidReal"][3]]

                        lower_left_of_second_box = [nn_out[j]["bboxReal"][0], nn_out[j]["bboxReal"][1],
                                                    nn_out[j]["centroidReal"][3]]
                        lower_right_of_second_box = [nn_out[j]["bboxReal"][2], nn_out[j]["bboxReal"][1],
                                                     nn_out[j]["centroidReal"][3]]
                        upper_left_of_second_box = [nn_out[j]["bboxReal"][0], nn_out[j]["bboxReal"][3],
                                                    nn_out[j]["centroidReal"][3]]
                        upper_right_of_second_box = [nn_out[j]["bboxReal"][2], nn_out[j]["bboxReal"][3],
                                                     nn_out[j]["centroidReal"][3]]

                        l1 = self.calculate_distance_of_two_points_of_boxes(lower_left_of_first_box,
                                                                            lower_left_of_second_box)
                        l2 = self.calculate_distance_of_two_points_of_boxes(lower_right_of_first_box,
                                                                            lower_right_of_second_box)
                        l3 = self.calculate_distance_of_two_points_of_boxes(upper_left_of_first_box,
                                                                            upper_left_of_second_box)
                        l4 = self.calculate_distance_of_two_points_of_boxes(upper_right_of_first_box,
                                                                            upper_right_of_second_box)

                        l = min(l1, l2, l3, l4)
                    elif (self.dist_method == 'CenterPointsDistance'):
                        center_of_first_box = [nn_out[i]["centroidReal"][0], nn_out[i]["centroidReal"][1],
                                               nn_out[i]["centroidReal"][3]]
                        center_of_second_box = [nn_out[j]["centroidReal"][0], nn_out[j]["centroidReal"][1],
                                                nn_out[j]["centroidReal"][3]]

                        l = self.calculate_distance_of_two_points_of_boxes(center_of_first_box, center_of_second_box)
                distance_row.append(l)
            distances.append(distance_row)
        distances_asarray = np.asarray(distances, dtype=np.float32)
        return distances_asarray
