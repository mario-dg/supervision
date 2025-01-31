import itertools
import math
import os
import shutil
from functools import partial
from typing import Callable, List, Literal, Optional, Tuple, Union

import cv2
import numpy as np

from supervision.annotators.base import ImageType
from supervision.draw.color import Color
from supervision.draw.utils import calculate_optimal_text_scale, draw_text
from supervision.geometry.core import Point
from supervision.utils.conversion import (
    convert_for_image_processing,
    cv2_to_pillow,
    images_to_cv2,
)
from supervision.utils.iterables import create_batches, fill

RelativePosition = Literal["top", "bottom"]

MAX_COLUMNS_FOR_SINGLE_ROW_GRID = 3


@convert_for_image_processing
def crop_image(image: np.ndarray, xyxy: np.ndarray) -> np.ndarray:
    """
    Crops the given image based on the given bounding box.

    Args:
        image (np.ndarray): The image to be cropped, represented as a numpy array.
        xyxy (np.ndarray): A numpy array containing the bounding box coordinates
            in the format (x1, y1, x2, y2).

    Returns:
        (np.ndarray): The cropped image as a numpy array.

    Examples:
        ```python
        import supervision as sv

        detection = sv.Detections(...)
        with sv.ImageSink(target_dir_path='target/directory/path') as sink:
            for xyxy in detection.xyxy:
                cropped_image = sv.crop_image(image=image, xyxy=xyxy)
                sink.save_image(image=cropped_image)
        ```
    """

    xyxy = np.round(xyxy).astype(int)
    x1, y1, x2, y2 = xyxy
    return image[y1:y2, x1:x2]


@convert_for_image_processing
def resize_image(image: np.ndarray, scale_factor: float) -> np.ndarray:
    """
    Resizes an image by a given scale factor using cv2.INTER_LINEAR interpolation.

    Args:
        image (np.ndarray): The input image to be resized.
        scale_factor (float): The factor by which the image will be scaled. Scale
            factor > 1.0 zooms in, < 1.0 zooms out.

    Returns:
        np.ndarray: The resized image.

    Raises:
        ValueError: If the scale factor is non-positive.
    """
    if scale_factor <= 0:
        raise ValueError("Scale factor must be positive.")

    old_width, old_height = image.shape[1], image.shape[0]
    nwe_width = int(old_width * scale_factor)
    new_height = int(old_height * scale_factor)

    return cv2.resize(image, (nwe_width, new_height), interpolation=cv2.INTER_LINEAR)


def place_image(
    scene: np.ndarray, image: np.ndarray, anchor: Tuple[int, int]
) -> np.ndarray:
    """
    Places an image onto a scene at a given anchor point, handling cases where
    the image's position is partially or completely outside the scene's bounds.

    Args:
        scene (np.ndarray): The background scene onto which the image is placed.
        image (np.ndarray): The image to be placed onto the scene.
        anchor (Tuple[int, int]): The (x, y) coordinates in the scene where the
            top-left corner of the image will be placed.

    Returns:
        np.ndarray: The modified scene with the image placed at the anchor point,
            or unchanged if the image placement is completely outside the scene.
    """
    scene_height, scene_width = scene.shape[:2]
    image_height, image_width = image.shape[:2]
    anchor_x, anchor_y = anchor

    is_out_horizontally = anchor_x + image_width <= 0 or anchor_x >= scene_width
    is_out_vertically = anchor_y + image_height <= 0 or anchor_y >= scene_height

    if is_out_horizontally or is_out_vertically:
        return scene

    start_y = max(anchor_y, 0)
    start_x = max(anchor_x, 0)
    end_y = min(scene_height, anchor_y + image_height)
    end_x = min(scene_width, anchor_x + image_width)

    crop_start_y = max(-anchor_y, 0)
    crop_start_x = max(-anchor_x, 0)
    crop_end_y = image_height - max((anchor_y + image_height) - scene_height, 0)
    crop_end_x = image_width - max((anchor_x + image_width) - scene_width, 0)

    scene[start_y:end_y, start_x:end_x] = image[
        crop_start_y:crop_end_y, crop_start_x:crop_end_x
    ]

    return scene


class ImageSink:
    def __init__(
        self,
        target_dir_path: str,
        overwrite: bool = False,
        image_name_pattern: str = "image_{:05d}.png",
    ):
        """
        Initialize a context manager for saving images.

        Args:
            target_dir_path (str): The target directory where images will be saved.
            overwrite (bool, optional): Whether to overwrite the existing directory.
                Defaults to False.
            image_name_pattern (str, optional): The image file name pattern.
                Defaults to "image_{:05d}.png".

        Examples:
            ```python
            import supervision as sv

            with sv.ImageSink(target_dir_path='target/directory/path',
                              overwrite=True) as sink:
                for image in sv.get_video_frames_generator(
                    source_path='source_video.mp4', stride=2):
                    sink.save_image(image=image)
            ```
        """

        self.target_dir_path = target_dir_path
        self.overwrite = overwrite
        self.image_name_pattern = image_name_pattern
        self.image_count = 0

    def __enter__(self):
        if os.path.exists(self.target_dir_path):
            if self.overwrite:
                shutil.rmtree(self.target_dir_path)
                os.makedirs(self.target_dir_path)
        else:
            os.makedirs(self.target_dir_path)

        return self

    def save_image(self, image: np.ndarray, image_name: Optional[str] = None):
        """
        Save a given image in the target directory.

        Args:
            image (np.ndarray): The image to be saved. The image must be in BGR color
                format.
            image_name (str, optional): The name to use for the saved image.
                If not provided, a name will be
                generated using the `image_name_pattern`.
        """
        if image_name is None:
            image_name = self.image_name_pattern.format(self.image_count)

        image_path = os.path.join(self.target_dir_path, image_name)
        cv2.imwrite(image_path, image)
        self.image_count += 1

    def __exit__(self, exc_type, exc_value, exc_traceback):
        pass


def create_tiles(
    images: List[ImageType],
    grid_size: Optional[Tuple[Optional[int], Optional[int]]] = None,
    single_tile_size: Optional[Tuple[int, int]] = None,
    tile_scaling: Literal["min", "max", "avg"] = "avg",
    tile_padding_color: Union[Tuple[int, int, int], Color] = Color.from_hex("#D9D9D9"),
    tile_margin: int = 10,
    tile_margin_color: Union[Tuple[int, int, int], Color] = Color.from_hex("#BFBEBD"),
    return_type: Literal["auto", "cv2", "pillow"] = "auto",
    titles: Optional[List[Optional[str]]] = None,
    titles_anchors: Optional[Union[Point, List[Optional[Point]]]] = None,
    titles_color: Union[Tuple[int, int, int], Color] = Color.from_hex("#262523"),
    titles_scale: Optional[float] = None,
    titles_thickness: int = 1,
    titles_padding: int = 10,
    titles_text_font: int = cv2.FONT_HERSHEY_SIMPLEX,
    titles_background_color: Union[Tuple[int, int, int], Color] = Color.from_hex(
        "#D9D9D9"
    ),
    default_title_placement: RelativePosition = "top",
) -> ImageType:
    """
    Creates tiles mosaic from input images, automating grid placement and
    converting images to common resolution maintaining aspect ratio. It is
    also possible to render text titles on tiles, using optional set of
    parameters specifying text drawing (see parameters description).

    Automated grid placement will try to maintain square shape of grid
    (with size being the nearest integer square root of #images), up to two exceptions:
    * if there are up to 3 images - images will be displayed in single row
    * if square-grid placement causes last row to be empty - number of rows is trimmed
        until last row has at least one image

    Args:
        images (List[ImageType]): Images to create tiles. Elements can be either
            np.ndarray or PIL.Image, common representation will be agreed by the
            function.
        grid_size (Optional[Tuple[Optional[int], Optional[int]]]): Expected grid
            size in format (n_rows, n_cols). If not given - automated grid placement
            will be applied. One may also provide only one out of two elements of the
            tuple - then grid will be created with either n_rows or n_cols fixed,
            leaving the other dimension to be adjusted by the number of images
        single_tile_size (Optional[Tuple[int, int]]): sizeof a single tile element
            provided in (width, height) format. If not given - size of tile will be
            automatically calculated based on `tile_scaling` parameter.
        tile_scaling (Literal["min", "max", "avg"]): If `single_tile_size` is not
            given - parameter will be used to calculate tile size - using
            min / max / avg size of image provided in `images` list.
        tile_padding_color (Union[Tuple[int, int, int], sv.Color]): Color to be used in
            images letterbox procedure (while standardising tiles sizes) as a padding.
            If tuple provided - should be BGR.
        tile_margin (int): size of margin between tiles (in pixels)
        tile_margin_color (Union[Tuple[int, int, int], sv.Color]): Color of tile margin.
            If tuple provided - should be BGR.
        return_type (Literal["auto", "cv2", "pillow"]): Parameter dictates the format of
            return image. One may choose specific type ("cv2" or "pillow") to enforce
            conversion. "auto" mode takes a majority vote between types of elements in
            `images` list - resolving draws in favour of OpenCV format. "auto" can be
            safely used when all input images are of the same type.
        titles (Optional[List[Optional[str]]]): Optional titles to be added to tiles.
            Elements of that list may be empty - then specific tile (in order presented
            in `images` parameter) will not be filled with title. It is possible to
            provide list of titles shorter than `images` - then remaining titles will
            be assumed empty.
        titles_anchors (Optional[Union[Point, List[Optional[Point]]]]): Parameter to
            specify anchor points for titles. It is possible to specify anchor either
            globally or for specific tiles (following order of `images`).
            If not given (either globally, or for specific element of the list),
            it will be calculated automatically based on `default_title_placement`.
        titles_color (Union[Tuple[int, int, int], Color]): Color of titles text.
            If tuple provided - should be BGR.
        titles_scale (Optional[float]): Scale of titles. If not provided - value will
            be calculated using `calculate_optimal_text_scale(...)`.
        titles_thickness (int): Thickness of titles text.
        titles_padding (int): Size of titles padding.
        titles_text_font (int): Font to be used to render titles. Must be integer
            constant representing OpenCV font.
            (See docs: https://docs.opencv.org/4.x/d6/d6e/group__imgproc__draw.html)
        titles_background_color (Union[Tuple[int, int, int], Color]): Color of title
            text padding.
        default_title_placement (Literal["top", "bottom"]): Parameter specifies title
            anchor placement in case if explicit anchor is not provided.

    Returns:
        ImageType: Image with all input images located in tails grid. The output type is
            determined by `return_type` parameter.

    Raises:
        ValueError: In case when input images list is empty, provided `grid_size` is too
            small to fit all images, `tile_scaling` mode is invalid.
    """
    if len(images) == 0:
        raise ValueError("Could not create image tiles from empty list of images.")
    if return_type == "auto":
        return_type = _negotiate_tiles_format(images=images)
    tile_padding_color = _color_to_bgr(color=tile_padding_color)
    tile_margin_color = _color_to_bgr(color=tile_margin_color)
    images = images_to_cv2(images=images)
    if single_tile_size is None:
        single_tile_size = _aggregate_images_shape(images=images, mode=tile_scaling)
    resized_images = [
        letterbox_image(
            image=i, desired_size=single_tile_size, color=tile_padding_color
        )
        for i in images
    ]
    grid_size = _establish_grid_size(images=images, grid_size=grid_size)
    if len(images) > grid_size[0] * grid_size[1]:
        raise ValueError(
            f"Could not place {len(images)} in grid with size: {grid_size}."
        )
    if titles is not None:
        titles = fill(sequence=titles, desired_size=len(images), content=None)
    titles_anchors = (
        [titles_anchors]
        if not issubclass(type(titles_anchors), list)
        else titles_anchors
    )
    titles_anchors = fill(
        sequence=titles_anchors, desired_size=len(images), content=None
    )
    titles_color = _color_to_bgr(color=titles_color)
    titles_background_color = _color_to_bgr(color=titles_background_color)
    tiles = _generate_tiles(
        images=resized_images,
        grid_size=grid_size,
        single_tile_size=single_tile_size,
        tile_padding_color=tile_padding_color,
        tile_margin=tile_margin,
        tile_margin_color=tile_margin_color,
        titles=titles,
        titles_anchors=titles_anchors,
        titles_color=titles_color,
        titles_scale=titles_scale,
        titles_thickness=titles_thickness,
        titles_padding=titles_padding,
        titles_text_font=titles_text_font,
        titles_background_color=titles_background_color,
        default_title_placement=default_title_placement,
    )
    if return_type == "pillow":
        tiles = cv2_to_pillow(image=tiles)
    return tiles


def _negotiate_tiles_format(images: List[ImageType]) -> Literal["cv2", "pillow"]:
    number_of_np_arrays = sum(issubclass(type(i), np.ndarray) for i in images)
    if number_of_np_arrays >= (len(images) // 2):
        return "cv2"
    return "pillow"


def _calculate_aggregated_images_shape(
    images: List[np.ndarray], aggregator: Callable[[List[int]], float]
) -> Tuple[int, int]:
    height = round(aggregator([i.shape[0] for i in images]))
    width = round(aggregator([i.shape[1] for i in images]))
    return width, height


SHAPE_AGGREGATION_FUN = {
    "min": partial(_calculate_aggregated_images_shape, aggregator=np.min),
    "max": partial(_calculate_aggregated_images_shape, aggregator=np.max),
    "avg": partial(_calculate_aggregated_images_shape, aggregator=np.average),
}


def _aggregate_images_shape(
    images: List[np.ndarray], mode: Literal["min", "max", "avg"]
) -> Tuple[int, int]:
    if mode not in SHAPE_AGGREGATION_FUN:
        raise ValueError(
            f"Could not aggregate images shape - provided unknown mode: {mode}. "
            f"Supported modes: {list(SHAPE_AGGREGATION_FUN.keys())}."
        )
    return SHAPE_AGGREGATION_FUN[mode](images)


def _establish_grid_size(
    images: List[np.ndarray], grid_size: Optional[Tuple[Optional[int], Optional[int]]]
) -> Tuple[int, int]:
    if grid_size is None or all(e is None for e in grid_size):
        return _negotiate_grid_size(images=images)
    if grid_size[0] is None:
        return math.ceil(len(images) / grid_size[1]), grid_size[1]
    if grid_size[1] is None:
        return grid_size[0], math.ceil(len(images) / grid_size[0])
    return grid_size


def _negotiate_grid_size(images: List[np.ndarray]) -> Tuple[int, int]:
    if len(images) <= MAX_COLUMNS_FOR_SINGLE_ROW_GRID:
        return 1, len(images)
    nearest_sqrt = math.ceil(np.sqrt(len(images)))
    proposed_columns = nearest_sqrt
    proposed_rows = nearest_sqrt
    while proposed_columns * (proposed_rows - 1) >= len(images):
        proposed_rows -= 1
    return proposed_rows, proposed_columns


def _generate_tiles(
    images: List[np.ndarray],
    grid_size: Tuple[int, int],
    single_tile_size: Tuple[int, int],
    tile_padding_color: Tuple[int, int, int],
    tile_margin: int,
    tile_margin_color: Tuple[int, int, int],
    titles: Optional[List[Optional[str]]],
    titles_anchors: List[Optional[Point]],
    titles_color: Tuple[int, int, int],
    titles_scale: Optional[float],
    titles_thickness: int,
    titles_padding: int,
    titles_text_font: int,
    titles_background_color: Tuple[int, int, int],
    default_title_placement: RelativePosition,
) -> np.ndarray:
    images = _draw_texts(
        images=images,
        titles=titles,
        titles_anchors=titles_anchors,
        titles_color=titles_color,
        titles_scale=titles_scale,
        titles_thickness=titles_thickness,
        titles_padding=titles_padding,
        titles_text_font=titles_text_font,
        titles_background_color=titles_background_color,
        default_title_placement=default_title_placement,
    )
    rows, columns = grid_size
    tiles_elements = list(create_batches(sequence=images, batch_size=columns))
    while len(tiles_elements[-1]) < columns:
        tiles_elements[-1].append(
            _generate_color_image(shape=single_tile_size, color=tile_padding_color)
        )
    while len(tiles_elements) < rows:
        tiles_elements.append(
            [_generate_color_image(shape=single_tile_size, color=tile_padding_color)]
            * columns
        )
    return _merge_tiles_elements(
        tiles_elements=tiles_elements,
        grid_size=grid_size,
        single_tile_size=single_tile_size,
        tile_margin=tile_margin,
        tile_margin_color=tile_margin_color,
    )


def _draw_texts(
    images: List[np.ndarray],
    titles: Optional[List[Optional[str]]],
    titles_anchors: List[Optional[Point]],
    titles_color: Tuple[int, int, int],
    titles_scale: Optional[float],
    titles_thickness: int,
    titles_padding: int,
    titles_text_font: int,
    titles_background_color: Tuple[int, int, int],
    default_title_placement: RelativePosition,
) -> List[np.ndarray]:
    if titles is None:
        return images
    titles_anchors = _prepare_default_titles_anchors(
        images=images,
        titles_anchors=titles_anchors,
        default_title_placement=default_title_placement,
    )
    if titles_scale is None:
        image_height, image_width = images[0].shape[:2]
        titles_scale = calculate_optimal_text_scale(
            resolution_wh=(image_width, image_height)
        )
    result = []
    for image, text, anchor in zip(images, titles, titles_anchors):
        if text is None:
            result.append(image)
            continue
        processed_image = draw_text(
            scene=image,
            text=text,
            text_anchor=anchor,
            text_color=Color.from_bgr_tuple(titles_color),
            text_scale=titles_scale,
            text_thickness=titles_thickness,
            text_padding=titles_padding,
            text_font=titles_text_font,
            background_color=Color.from_bgr_tuple(titles_background_color),
        )
        result.append(processed_image)
    return result


def _prepare_default_titles_anchors(
    images: List[np.ndarray],
    titles_anchors: List[Optional[Point]],
    default_title_placement: RelativePosition,
) -> List[Point]:
    result = []
    for image, anchor in zip(images, titles_anchors):
        if anchor is not None:
            result.append(anchor)
            continue
        image_height, image_width = image.shape[:2]
        if default_title_placement == "top":
            default_anchor = Point(x=image_width / 2, y=image_height * 0.1)
        else:
            default_anchor = Point(x=image_width / 2, y=image_height * 0.9)
        result.append(default_anchor)
    return result


def _merge_tiles_elements(
    tiles_elements: List[List[np.ndarray]],
    grid_size: Tuple[int, int],
    single_tile_size: Tuple[int, int],
    tile_margin: int,
    tile_margin_color: Tuple[int, int, int],
) -> np.ndarray:
    vertical_padding = (
        np.ones((single_tile_size[1], tile_margin, 3)) * tile_margin_color
    )
    merged_rows = [
        np.concatenate(
            list(
                itertools.chain.from_iterable(
                    zip(row, [vertical_padding] * grid_size[1])
                )
            )[:-1],
            axis=1,
        )
        for row in tiles_elements
    ]
    row_width = merged_rows[0].shape[1]
    horizontal_padding = (
        np.ones((tile_margin, row_width, 3), dtype=np.uint8) * tile_margin_color
    )
    rows_with_paddings = []
    for row in merged_rows:
        rows_with_paddings.append(row)
        rows_with_paddings.append(horizontal_padding)
    return np.concatenate(
        rows_with_paddings[:-1],
        axis=0,
    ).astype(np.uint8)


def _generate_color_image(
    shape: Tuple[int, int], color: Tuple[int, int, int]
) -> np.ndarray:
    return np.ones(shape[::-1] + (3,), dtype=np.uint8) * color


@convert_for_image_processing
def letterbox_image(
    image: np.ndarray,
    desired_size: Tuple[int, int],
    color: Union[Tuple[int, int, int], Color] = (0, 0, 0),
) -> np.ndarray:
    """
    Resize and pad image to fit the desired size, preserving its aspect
    ratio, adding padding of given color if needed to maintain aspect ratio.

    Args:
        image (np.ndarray): Input image (type will be adjusted by decorator,
            you can provide PIL.Image)
        desired_size (Tuple[int, int]): image size (width, height) representing
            the target dimensions.
        color (Union[Tuple[int, int, int], Color]): the color to pad with - If
            tuple provided - should be BGR.

    Returns:
        np.ndarray: letterboxed image (type may be adjusted to PIL.Image by
            decorator if function was called with PIL.Image)
    """
    color = _color_to_bgr(color=color)
    resized_img = resize_image_keeping_aspect_ratio(
        image=image,
        desired_size=desired_size,
    )
    new_height, new_width = resized_img.shape[:2]
    top_padding = (desired_size[1] - new_height) // 2
    bottom_padding = desired_size[1] - new_height - top_padding
    left_padding = (desired_size[0] - new_width) // 2
    right_padding = desired_size[0] - new_width - left_padding
    return cv2.copyMakeBorder(
        resized_img,
        top_padding,
        bottom_padding,
        left_padding,
        right_padding,
        cv2.BORDER_CONSTANT,
        value=color,
    )


@convert_for_image_processing
def resize_image_keeping_aspect_ratio(
    image: np.ndarray,
    desired_size: Tuple[int, int],
) -> np.ndarray:
    """
    Resize and pad image preserving its aspect ratio.

    For example: input image is (640, 480) and we want to resize into
    (1024, 1024). If this rectangular image is just resized naively
    to square-shape output - aspect ratio would be altered. If we do not
    want this to happen - we may resize bigger dimension (640) to 1024.
    Ratio of change is 1.6. This ratio is later on used to calculate scaling
    in the other dimension. As a result we have (1024, 768) image.

    Parameters:
    - image (np.ndarray): Input image (type will be adjusted by decorator,
        you can provide PIL.Image)
    - desired_size (Tuple[int, int]): image size (width, height) representing the
        target dimensions. Parameter will be used to dictate maximum size of
        output image. Output size may be smaller - to preserve aspect ratio of original
        image.

    Returns:
        np.ndarray: resized image (type may be adjusted to PIL.Image by decorator
            if function was called with PIL.Image)
    """
    if image.shape[:2] == desired_size[::-1]:
        return image
    img_ratio = image.shape[1] / image.shape[0]
    desired_ratio = desired_size[0] / desired_size[1]
    if img_ratio >= desired_ratio:
        new_width = desired_size[0]
        new_height = int(desired_size[0] / img_ratio)
    else:
        new_height = desired_size[1]
        new_width = int(desired_size[1] * img_ratio)
    return cv2.resize(image, (new_width, new_height))


def _color_to_bgr(color: Union[Tuple[int, int, int], Color]) -> Tuple[int, int, int]:
    if issubclass(type(color), Color):
        return color.as_bgr()
    return color
