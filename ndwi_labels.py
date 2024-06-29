# import libraries
import rasterio as rio
from rasterio import mask
from rasterio.plot import show
from rasterio.mask import mask
from rasterio.io import MemoryFile
import shapely
from shapely.geometry import Polygon, shape
import geopandas as gpd
import os
from matplotlib import pyplot as plt
import numpy as np
import cv2

def create_transect_points(transect_path, line_path, out_path):
    transects = gpd.read_file(transect_path)
    coastline = gpd.read_file(line_path)
    points = coastline.unary_union.intersection(transects.unary_union)
    fig, ax = plt.subplots(figsize=(14,14))
    plot_points = gpd.GeoSeries(points)
    plot_points.plot(ax=ax, color='green')
    transects.plot(ax=ax, color='red')
    coastline.plot(ax=ax, color='blue')

    plt.show()

    plot_points.to_file(out_path)


def clip_shp(path_to_shp, boundary_geojson):
    path_name = os.path.dirname(path_to_shp) + "/"
    shp_name = os.path.basename(path_to_shp)
    shp_base, shp_extension = os.path.splitext(shp_name)
    shp_data = gpd.read_file(path_to_shp)

    poly_boundary = Polygon(shape(boundary_geojson))

    shp_clipped = gpd.clip(shp_data, poly_boundary)
    fig, ax = plt.subplots(figsize=(12,8))
    shp_data.plot(ax=ax, color='red')
    plot_shp = gpd.GeoSeries(poly_boundary)
    plot_shp.plot(ax=ax, color='green')
    plt.show()

    out_path = path_name + shp_base + "_clipped.shp"
    shp_clipped.to_file(out_path)


# Gaussian blur parameters
ksize_blur = (13, 13)  # Kernel size (must be positive and odd)
sigmaX = 8      # Standard deviation in X direction
sigmaY = 8      # Standard deviation in Y direction


def get_ndwi_label(image_path, points_path, ksize = 100, blurring = True):
    """
    This function performs NDWI calculation and classification with optional Gaussian blurring.

    Steps:
    1. Read green and NIR bands to calculate NDWI.
    2. Apply Gaussian blurring if required.
    3. Match points CRS with Raster.
    4. Create a buffer around each point (number of buffers depend on number of points in shapefile).
    5. Mask the NDWI image with the specified buffer (sliding window).
        5.1 out_image: same shape as original image with specified buffer.
        5.2 out_image_clipped: clipped to the buffer.
            If the entire buffer is inside the NDWI image, its shape should be (201,201).
            If all or part of the sliding window is outside the NDWI image, the shape will be smaller.
    6. Skip sliding windows that are not entirely inside the NDWI image.
    7. Calculate threshold based on the clipped image.
    8. Apply OR operations between `out_image` to form the final labeled image.
    9. It is important to note that the NDWI threshold value may be less than 128.
       Therefore, it is crucial to substitute no data with -1.
    """
    # establish the ndwi calculation and copy metadata
    with rio.open(image_path, driver='GTiff') as src_raster:
        
        green = src_raster.read(2).astype(np.float32) # Get the green band 
        nir_num = src_raster.count  # Adjusting NIR band to 4 or 5 band images
        nir = src_raster.read(nir_num).astype(np.float32) # Get NIR band
        
        np.seterr(divide='ignore', invalid='ignore')
        
        ndwi = (green - nir) / (green + nir)  # NDWI equation
        ndwi[np.isnan(ndwi)] = 0 # Sets any NaN values in the NDWI array to 0. (Dividing by zero => Nan Pixels)
        ndwi_profile = src_raster.profile # Copies the image profile (metadata).
        
        # Apply Gaussian blur
        if blurring:
            print("Gaussian Filtering Applied")
            ndwi = cv2.GaussianBlur(ndwi, ksize_blur, sigmaX, sigmaY)
        
        # blank label layer
        label = np.zeros((src_raster.height, src_raster.width)).astype(np.uint8)
        src_CRS = src_raster.crs
        
        # Getting pixel size for correct calculation of buffer.
        # This value express spatial resolution.
        pixel_size = abs(src_raster.transform[0]) 
        
    # preparing points for creating label masks
    points_shp = gpd.read_file(points_path)
    points_geom = points_shp.geometry
    points_geom = points_geom.set_crs(epsg=4326) # Set CRS to WGS84
    points_geom = points_geom.to_crs(src_CRS) # Convert CRS to match the raster


    otsu_thresholds_clipped = [] # Creating a holder for Otsu's threshold values for clipped images
    skipped = 0 # Counter for skipped windows (Less than 201*201)
    
    # processing each point found
    for multipoint in points_geom:
        for point in multipoint.geoms:
            # Create a buffer around the point
            buffer = point.buffer(ksize * pixel_size, cap_style=3)
            buffer_series = gpd.GeoSeries(buffer)

            # writing NDWI to an in-memory dataset to use for masking
            ndwi_profile.update(count=1, nodata=0, dtype=rio.float32)
            with MemoryFile() as memfile:
                with memfile.open(**ndwi_profile) as mem_data:
                    mem_data.write_band(1, ndwi)
                with memfile.open() as dataset:
                
                    out_image, out_transform = mask(dataset, shapes=[buffer], nodata=-1, crop=False)
                    out_image = out_image[0]
                    out_image = (out_image * 127) + 128
                    out_image = out_image.astype(np.uint8)
                    
                    out_image_clipped, out_transform_clipped = mask(dataset, shapes=[buffer], nodata=-1, crop=True)
                    out_image_clipped = out_image_clipped[0]
                    out_image_clipped = (out_image_clipped * 127) + 128
                    out_image_clipped = out_image_clipped.astype(np.uint8)
                
                # Skip buffering windows that part or all of it out of NDWI image
                if out_image_clipped.shape[0] < 200 or out_image_clipped.shape[1] < 200:
                    skipped += 1
                    continue
                
                else:
                    # Calculate otsu threshold based on clipped image
                    threshold_clipped, image_result_clipped = cv2.threshold(out_image_clipped, 0, 1, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                    otsu_thresholds_clipped.append(threshold_clipped)
                    threshold_window = np.where(out_image >= threshold_clipped, 1, 0).astype(np.uint8)
                    label = label | threshold_window.astype(np.uint8)
                        
    print(f"Green min: {green.min():.2f}, Green max: {green.max():.2f}")
    print(f"NIR min: {nir.min():.2f}, NIR max: {nir.max():.2f}")
    print(f"NDWI min: {ndwi.min():.2f}, NDWI max: {ndwi.max():.2f}") # From -1 to +1
        
    print(f"Total number of valid thresholds: {len(otsu_thresholds_clipped)}")
    print(f"Number of skipped windows: {skipped}")
    
    print(f"Actual thresholds (8-bit unsigned): {otsu_thresholds_clipped}")
    print(f"Average threshold value (8-bit unsigned): {np.mean(otsu_thresholds_clipped)}")
    print(f"Average threshold value (-1 to 1 NDWI range): {(np.mean(otsu_thresholds_clipped) - 128) / 127}")

    print(f"Label min: {np.nanmin(label)} , max: {np.nanmax(label)}")

    mean_threshold = np.mean(otsu_thresholds_clipped) + 10
    ndwi_8bit = ((ndwi * 127) + 128).astype(np.uint8)
    ndwi_classified = np.where(ndwi_8bit >= mean_threshold, 1, 0)
    
    # Assuming 'ndwi' is your image data
    plt.imshow(ndwi)
    plt.title('NDWI image ')
    plt.show()
    
    # Plotting images side by side
    fig, axs = plt.subplots(1, 2, figsize=(10, 5))

    # Plot label image
    axs[0].imshow(label)
    axs[0].set_title('NDWI Classified with sliding window')
    axs[0].axis('off')

    # Plot NDWI classified image
    axs[1].imshow(ndwi_classified, cmap="gray")
    axs[1].set_title('NDWI Classified with mean threshold')
    axs[1].axis('off')

    plt.show()
            


boundary = {'type': 'Polygon',
            'coordinates': [[[-162.8235626220703, 66.05622435812153], 
                             [-162.674560546875, 66.05622435812153], 
                             [-162.674560546875, 66.10883816429516],
                             [-162.8235626220703, 66.10883816429516], 
                             [-162.8235626220703, 66.05622435812153]]]}
image_path = "D:/GSoC2024/data/input/268898_0369619_2016-10-15_0e14_BGRN_SR_clip.tif"
points_path = "D:/GSoC2024/data/Deering2016/Deering_transect_points_2016.shp"
get_ndwi_label(image_path, points_path)
