class Detector():
    """
    Detector class is a high level class for detecting object using NVIDIA jetson devices.
    When an instance of the Detector is created you can call inference method and feed your
    input image in order to get the detection results.

    :param config: Is a ConfigEngine instance which provides necessary parameters.
    """

    def __init__(self, config):
        self.config = config
        self.net = None
        # Get model name from the config
        self.name = self.config.get_section_dict('Detector')['Name']
        if self.name == 'ssd_mobilenet_v2_coco':
            from . import mobilenet_ssd_v2
            self.net = mobilenet_ssd_v2.Detector(self.config)
        else:
            raise ValueError('Not supported network named: ', self.name)

    def inference(self, resized_rgb_image):
        # Here should inference on the image, output a list of objects, each obj is a dict with two keys "id" and "bbox" and "score" 
        # return [{"id": 0, "bbox": [x1, y1, x2, y2], "score":s%}, {...}, {...}, ...]
        return self.net.inference(resized_rgb_image)
