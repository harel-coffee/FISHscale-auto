import numpy as np
import pandas as pd
from sklearn.neighbors import KDTree
from scipy.stats import spearmanr
import warnings
from dask.diagnostics import ProgressBar
import dask
import math
from scipy.ndimage import binary_erosion
from typing import Tuple
from scipy.spatial import distance
from tqdm import tqdm
import logging

from skimage.feature import local_binary_pattern
from scipy.ndimage import gaussian_filter, laplace
from skimage.restoration import denoise_nl_means, estimate_sigma
from skimage.filters import laplace
from dask import delayed, compute
from dask.diagnostics import ProgressBar

def _worker_bisect(points: np.ndarray, grid: np.ndarray, radius: float, 
                   lines: list, n_angles: int):
    """Bisect cloud of points around grid points.
    
    For a set of points and an overlaying grid, find which points are within
    the radius of every grid point. Afterwards it devides the points with a 
    bisection line for various angles and counts the number of points on 
    either side.
    Args:
        points (np.ndarray): Numpy array or Pandas dataframe with XY
            coordinates for points.
        grid (np.ndarray): XY coordinates of grid points.
        radius (float): Search radius around each grid point. Radius can be
            larger than grid spacing.
        lines (list): List with two numpy arrays with the XY coordinates of
            the start and end coordinates of a line that bisects the circle.
            (Output of the self.bisect() function)
        n_angles (int): Number of angles to test.
        
    Returns:
    angle_counts (np.ndarray): For each grid point the count of points above 
        the bisection line.
    count (np.ndarray): Total count for each grid point. Can be used to
        calculate the number of points below the bisection line.
    
    """
    def isabove(p, line):
        "Find points that are above bisection line."
        return np.cross(p-line[0], line[1]-line[0]) < 0

    #Build the tree
    tree = KDTree(points)

    #Query the grid for points within the radius
    index = tree.query_radius(grid, radius)

    #Count number of molecules
    count = np.array([i.shape[0] for i in index])

    #Get coordinates
    coords = [points.iloc[i].to_numpy() for i in index]
    
    #find how many are in each half
    angle_counts = np.zeros((grid.shape[0], n_angles), dtype='int32')

    #Iterate over grid points
    for i, (c,g) in enumerate(zip(coords, grid)):
        if c.shape[0] > 0:
            #Center the points to (0,0)
            cc = c - g

            #Iterate over angles
            for j, l in enumerate(lines):
                angle_counts[i, j] = isabove(cc, l).sum()

    #Return count for each half, and total count
    return angle_counts, count 

def _worker_angle(n_angles: int, d1: np.ndarray, d2: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Calculate euclidian distance for all devisions of the points in a circle.
    Args:
        n_angles (int): Number of angles to devide the circle by.
        d1 (np.ndarray): Count of point above the bisection lines.
        d2 (np.ndarray): Count of points below the bisection lines.
    Returns:
        Tuple[np.ndarray, np.ndarray]: [description]
    """
    r = np.zeros(n_angles)
    dist = np.zeros(n_angles)
    for j in range(n_angles):
        #Catch warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            dist[j] = distance.euclidean(d1[:,j], d2[:,j])
    
    return dist.max(), dist.argmax()

class Boundaries:
    
    def bisect(self, r: float, a: float, offset=None) -> Tuple[np.ndarray, np.ndarray]:
        """make line that bisects a circle.
        
        Returns the start and end coordinates of a line that bisects a circle
        with its center at (0,0)
        Args:
            r (float): Radius of circle.
            a (float): Angle in degree.
            offset (np.ndarray): Offset from center coordinate, which normally
                is at(0,0) .
        Returns:
            Tuple[np.ndarray, np.ndarray]:
            XY coordinate of start of line.
            XY coordinate of end of line.
        """
        a = np.deg2rad(a)
        p = np.array([np.cos(a) * r, np.sin(a) * r])
        q = -p
        if not offset is None:
            p += offset
            q += offset
            
        return p, q
    
    def square_grid(self, bin_size:float, x_extent:float, y_extent:float, x_min:float, x_max:float, y_min:float, 
                    y_max:float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Make square grid that matches the shape of a dataset.
        
        Extends the grid to match the extent of the dataset so that the grid 
        point evenly cover the dataset.
        Args:
            bin_size (float): Distance between grid points.
            x_extent (float): X extent of the dataset.
            y_extent (float): Y extent of the dataset.
            x_min (float): X minimimum of dataset.
            x_max (float): X maximum of dataset.
            y_min (float): Y minimum of dataset.
            y_max (float): Y maximum of dataset.
        Returns:
            Tuple[np.ndarray, np.ndarray, np.ndarray]: 
            Grid: Array with grid XY positions.
            Xi: X coordinates in shape of grid.
            Yi: Y coordinates in shape of grid.
        """

        # Create grid values
            #Adjust X extend to make regular grid
        nx = math.ceil(x_extent / bin_size)
        d_x = (nx * bin_size) - x_extent
        xi = np.linspace(x_min - (0.5 * d_x), x_max + (0.5 * d_x), nx)
            #Adjust X extend to make regular grid
        ny = math.ceil(y_extent / bin_size)
        d_y = (ny * bin_size) - y_extent
        yi = np.linspace(y_min - (0.5 * d_y), y_max + (0.5 * d_y), ny)
            #Make grid
        Xi, Yi = np.meshgrid(xi, yi)
        grid = np.column_stack((Xi.ravel(), Yi.ravel()))

        return grid, Xi, Yi

    def boundaries_make(self, bin_size: int = 100, radius: int = 200, n_angles: int = 6,
                normalize: bool = False, normalization_mode: str = 'log', gene_selection: any = None, 
                n_jobs: int = -1) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Calculate local similarity to investigate border strenth.
        
        Overlays the sample with a grid of points. For each point the molecules
        within the radius are selected. This circle is then devided in two 
        halves, at different angles. The Euclidian distance between the 
        molecule counts of the two halves is calcuated and the value and angle
        of the angle with the highest distance is returned.
        
        The higher the distance the stronger the border is, and the angle 
        indicates the direction of the (potential) border.
        
        An array of the resulting angle, similar to the image output,
        can be made using:
        np.zeros(shape)[filt_grid] = borders[:,1]
        
        Args:
            bin_size (int, optional): The distance between the grid points in 
                the same unit as the dataset. Defaults to 100.
            radius (int, optional): Search radius for asigning molecules to 
                grid points. May be larger than the bin_size. Defaults to 200.
            n_angles (int, optional): Number of angles to test. Defaults to 6.
            normalize (bool, optional): If True normalizes the count data.
                Defaults to False.
            normalization_mode (str, optional): Normalization method to use.
                Defaults to 'log'.
            gene_selection (list, np.ndarray, optional): Genes 
            n_jobs (int, optional): Number of processes to use. If None, 
                the max number of cpus is used. Defaults to None.
        Returns:
            Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
            results: Array with in the first colum the euclidian distance and 
                in the second column the angle with the highest distance.
                These are only caluclated for grid points that fall within 
                the dataset and the point coordinates can be found in 
                `grid_filt`.
            image: Array of the results in the shape of the tissue.
            grid: XY coordinates of all grid points. 
            grid_filt: XY coordinates of valid grid points.
            filt_grid: boolean filter to filter the `grid` to get `grid_filt`.
            shape: Original shape of grid. 
        """
        if n_jobs == None:
            n_jobs = self.cpu_count()
        
        #Make the grid overlaying the data       
        grid, Xi, Yi = self.square_grid(bin_size, self.x_extent, self.y_extent, 
                                        self.x_min, self.x_max, self.y_min, self.y_max)
        shape = Xi.shape

        #make lines that bisect a circle for each required angle
        lines = [self.bisect(radius, a) for a in np.linspace(0, 180 - (180 / n_angles), n_angles)]

        #Fetch genes to run
        if type(gene_selection) == type(None):
            genes = self.unique_genes
        else:
            genes = gene_selection
        
        #Find number of molecules in each division
        results = []
        for g in genes:
            points = self.get_gene(g)
            y = dask.delayed(_worker_bisect)(points, grid, radius, lines, n_angles)
            results.append(y)

        #Compute
        logging.info('Computation 1 / 2: ') 
        with ProgressBar():
            results = dask.compute(*results, scheduler='processes', n_workers=n_jobs)
        logging.info('Computation 2 / 2: ') 

        #Identify grid points without molecules
        count_matrix = np.array([i[1] for i in results])
        results_angle_counts = [i[0] for i in results]
        filt = count_matrix.sum(axis=0) > 0
        #Remove contour
        filt_grid = filt.reshape(Xi.shape[0], Xi.shape[1])
        filt_grid = binary_erosion(filt_grid, iterations=math.ceil(radius / bin_size))
        filt = filt_grid.ravel()
        
        #Filter data
        results_angle_counts = [r[filt] for r in results_angle_counts]
        count_matrix = count_matrix[:, filt]
        grid_filt = grid[filt, :]

        #Calculate counts of other half of the circle
        stack = np.stack(results_angle_counts)
        cm_repeat = np.repeat(count_matrix[:,:,np.newaxis], n_angles, axis=2)
        stack_other = cm_repeat - stack

        #Normalize the data
        if normalize:
            #Normalize the count matrix
            cm_norm = self.normalize(pd.DataFrame(count_matrix), mode=normalization_mode).to_numpy()
            cm_norm = np.repeat(cm_norm[:,:,np.newaxis], n_angles, axis=2)
            #Take fraction in hemicircle and multiply with normalized data
            stack = cm_norm * np.nan_to_num((stack / cm_repeat))
            stack_other = cm_norm * np.nan_to_num((stack_other / cm_repeat))

        #Calculate correlation coefficient and pick angle with lowest correlation coefficient
        results2 = []
        for i in range(count_matrix.shape[1]):
            d1 = stack[:,i,:]
            d2 = stack_other[:,i,:]
            z = dask.delayed(_worker_angle)(n_angles, d1, d2)
            results2.append(z)
            
        #Compute
        with ProgressBar():
            results2 = dask.compute(*results2, scheduler='processes')
        results2 = np.array(results2)
        results2 = np.nan_to_num(results2)
        
        #Convert angle index to angle in degree
        angle_used = (np.linspace(0, 180 - (180/n_angles), n_angles))
        results2[:,1] = [angle_used[int(i)] for i in results2[:,1]]
        #results2[:,3] = [angle_used[int(i)] for i in results2[:,3]]
        
        image = np.zeros(shape)
        image[filt_grid] = results2[:,0]
        
        return results2, image, grid, grid_filt, filt_grid, shape
    
    ###########################################################################
    # LBP boundaries
    
    def _LBP_worker(self, img: np.ndarray, mask: np.ndarray, 
                    h_factor: int=5, patch_size: int=5, patch_distance :int=6,
                    n_points: int=10, radius: int=2) -> np.ndarray:
        """Worker function for LBP calculation. 
        
        See boundaries_LBP_make for documentation on in and output.
        """
        
        #Denoise data
        sigma_est = np.mean(estimate_sigma(img))
        denoise = denoise_nl_means(img, 
                                   h= h_factor*sigma_est, 
                                   fast_mode = True, 
                                   sigma = sigma_est, 
                                   preserve_range = True, 
                                   patch_size = patch_size, 
                                   patch_distance = patch_distance)
        denoise[~mask] = 0

        #Calculate Local Binary Pattern
        lbp = local_binary_pattern(denoise, 
                                   n_points, 
                                   radius,
                                   'var')

        #Clean output
        lbp[np.isnan(lbp)] = 0
        lbp[~mask] = np.nan
        
        return lbp
                
    
    def boundaries_LBP_make(self, squarebin: np.ndarray, mask: np.ndarray,
                            h_factor: int=5, patch_size: int=5, 
                            patch_distance :int=6,
                            n_points: int=12, radius: float=1.5) -> np.ndarray:
        """Calculate gene boundaries using Local Binary Patterns (LBP).
        
        Calculates the boundary strength for each gene using LBP. Please see
        skimage.feature.local_binary_pattern() for more background. 
        Data is denoised using non-local means. See 
        skimage.restoration.denoise_nl_means() for more details. 

        Args:
            squarebin (np.ndarray): Array with the binned gene counts.
            mask (np.ndarray): Array with a boolean mask of the valid data 
                pixels.
            h_factor (int): Factor to multiply the calculated sigma with. For
                denoising the size of the standard deviation (sigma) of the
                noise is calculated. This factor multiplies the sigma so that
                the image is denoised with a larger sigma to remove background.
                For more information see skimage.restoration.estimate_sigma()
                and skimage.restoration.denoise_nl_means().
                Defaults to 5.
            patch_size (int): Size of the patches used for denoising.
                See skimage.restoration.denoise_nl_means() for more details.
                Defaults to 5
            patch_distance (int): Maximal distance in pixels where to search 
                patches used for denoising.
                See skimage.restoration.denoise_nl_means() for more details.
                Defaults to 6
            n_points (int, optional): Number of points to calculate the LBP on.
                Number of circularly symmetric neighbor set points 
                (quantization of the angular space). Defaults to 12.
            radius (float, optional): Radius of the LBP. Radius of circle 
                (spatial resolution of the operator). The unit is pixels.
                Defaults to 1.5.

        Returns:
            np.ndarray: Array in the shape (X, Y, n_genes) with the results.
                The genes in the last axis are in the same order as 
                self.unique_genes.

        """
        
        n_genes = squarebin.shape[-1]
        LBP_results = []
        for i in range(n_genes):
            r = delayed(self._LBP_worker)(squarebin[:,:,i], mask, h_factor, patch_size, patch_distance, n_points, radius)
            LBP_results.append(r)
        
        with ProgressBar():
            results = compute(*LBP_results)
        
        return np.stack(results, axis=2)
        
        
    
class Boundaries_Multi:
    
    def boundaries_make_multi(self, bin_size: int = 100, radius: int = 200, n_angles: int = 6,
                normalize: bool = False, normalization_mode: str = 'log', gene_selection: any = None, 
                n_jobs: int = -1):
        """Calculate local similarity to investigate border strenth.
        
        Loops over all samples in the multi-dataset and for each sample 
        overlays the sample with a grid of points. For each point the molecules
        within the radius are selected. This circle is then devided in two 
        halves, at different angles. The Euclidian distance between the 
        molecule counts of the two halves is calcuated and the value and angle
        of the angle with the highest distance is returned.
        
        The higher the distance the stronger the border is, and the angle 
        indicates the direction of the (potential) border.
        
        Results are stored in a dictionary per dataset and can be accessed
        easily using self.get_dict_item().
        
        An array of the resulting angle, similar to the image output,
        can be made using:
        np.zeros(shape)[filt_grid] = borders[:,1]
        
        Args:
            bin_size (int, optional): The distance between the grid points in 
                the same unit as the dataset. Defaults to 100.
            radius (int, optional): Search radius for asigning molecules to 
                grid points. May be larger than the bin_size. Defaults to 200.
            n_angles (int, optional): Number of angles to test. Defaults to 6.
            normalize (bool, optional): If True normalizes the count data.
                Defaults to False.
            normalization_mode (str, optional): Normalization method to use.
                Defaults to 'log'.
            gene_selection (list, np.ndarray, optional): Genes 
            n_jobs (int, optional): Number of processes to use. If None, 
                the max number of cpus is used. Defaults to None.
        Returns:
            Returns:
            Dictionary containing:
                - results: Array with in the first colum the euclidian distance
                    and in the second column the angle with the highest 
                    distance. These are only caluclated for grid points that 
                    fall within the dataset and the point coordinates can be 
                    found in `grid_filt`.
                - image: Array with the border strength as values.
                - grid: XY coordinates of all grid points. 
                - grid_filt: XY coordinates of valid grid points.
                - filt_grid: boolean filter to filter the `grid` to get 
                    `grid_filt`.
                - shape: Original shape of grid.
        """
        
        results = {}
        for d in tqdm(self.datasets):
            results[d.dataset_name] = {}

            r, image, grid, grid_filt, filt_grid, shape = d.boundaries_make(bin_size = bin_size,
                                                              radius = radius,
                                                              n_angles = n_angles,
                                                              normalize = normalize,
                                                              normalization_mode = normalization_mode,
                                                              gene_selection = gene_selection,
                                                              n_jobs = n_jobs)
            
            results[d.dataset_name]['result'] = r
            results[d.dataset_name]['image'] = image
            results[d.dataset_name]['grid'] = grid
            results[d.dataset_name]['grid_filt'] = grid_filt
            results[d.dataset_name]['filt_grid'] = filt_grid
            results[d.dataset_name]['shape'] = shape
            
        return results
    
    def boundaries_LBP_make_multi(self, squarebin: list, 
                                  masks: list, h_factor: int=5, 
                                  patch_size: int=5, patch_distance :int=6,
                                  n_points: int=12, 
                                  radius: float=1.5) -> list:
        """Calculate gene boundaries using Local Binary Patterns (LBP).
        
        Calculates the boundary strength for each gene using LBP. Please see
        skimage.feature.local_binary_pattern() for more background. 
        Data is denoised using non-local means. See 
        skimage.restoration.denoise_nl_means() for more details. 

        Args:
            squarebin (list): List of arrays with the binned gene counts. 
                Arrays should have the shape (X, Y, n_genes)
            masks (list): List with arrays with a boolean mask of the valid data 
                pixels.
            h_factor (int): Factor to multiply the calculated sigma with. For
                denoising the size of the standard deviation (sigma) of the
                noise is calculated. This factor multiplies the sigma so that
                the image is denoised with a larger sigma to remove background.
                For more information see skimage.restoration.estimate_sigma()
                and skimage.restoration.denoise_nl_means().
                Defaults to 5.
            patch_size (int): Size of the patches used for denoising.
                See skimage.restoration.denoise_nl_means() for more details.
                Defaults to 5
            patch_distance (int): Maximal distance in pixels where to search 
                patches used for denoising.
                See skimage.restoration.denoise_nl_means() for more details.
                Defaults to 6
            n_points (int, optional): Number of points to calculate the LBP on.
                Number of circularly symmetric neighbor set points 
                (quantization of the angular space). Defaults to 12.
            radius (float, optional): Radius of the LBP. Radius of circle 
                (spatial resolution of the operator). The unit is pixels.
                Defaults to 1.5.

        Returns:
            list : List of array in the shape (X, Y, n_genes) with the results.
                The genes in the last axis are in the same order as 
                self.unique_genes.

        """
        results = []
        for dd, img, mask in zip(self.datasets, squarebin, masks):
            results.append(dd.boundaries_LBP_make(img, mask, h_factor, patch_size, patch_distance, n_points, radius))
        
        return results