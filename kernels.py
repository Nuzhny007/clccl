from kernel_common import *

class CCL(object):
    def __init__(self, img_size, img_dtype, label_dtype, connectivity_dtype=np.uint32, debug=False, best_wg_size = default_wg_size):
        self.img_size = img_size
        self.img_dtype = img_dtype
        self.label_dtype = label_dtype
        self.connectivity_dtype = connectivity_dtype
        self.debug = debug
        self.best_wg_size = best_wg_size

        self.img_size = np.asarray(img_size, np.uint32)
        self.program = None
        self.kernel = None
        self.WORKGROUP_TILE_SIZE_X = 32
        self.WORKGROUP_TILE_SIZE_Y = 2
        self.WORKITEM_REPEAT_X     = 1
        self.WORKITEM_REPEAT_Y     = 16
        self.TILE_ROWS = self.WORKGROUP_TILE_SIZE_Y * self.WORKITEM_REPEAT_Y
        self.TILE_COLS = self.WORKGROUP_TILE_SIZE_X * self.WORKITEM_REPEAT_X
        self.COMPACT_TILE_ROWS = 32
        self.COMPACT_TILE_COLS = 8

    def make_input_buffer(self, queue):
        return clarray.empty(queue, tuple(self.img_size), dtype=self.img_dtype)

    def make_host_output_buffer(self):
        return np.empty(self.img_size, dtype=self.label_dtype)

    def compile(self):
        PixelT = type_mapper(self.img_dtype)
        LabelT = type_mapper(self.label_dtype)

        KERNEL_FLAGS = '-D PIXELT={PixelT} -D LABELT={LabelT} -D WORKGROUP_TILE_SIZE_X={wg_tile_size_x} -D WORKGROUP_TILE_SIZE_Y={wg_tile_size_y} -D WORKITEM_REPEAT_X={wi_repeat_x} -D WORKITEM_REPEAT_Y={wi_repeat_y}' \
            .format(PixelT=PixelT, LabelT=LabelT, wg_tile_size_x=self.WORKGROUP_TILE_SIZE_X, wg_tile_size_y=self.WORKGROUP_TILE_SIZE_Y, wi_repeat_y=self.WORKITEM_REPEAT_Y, wi_repeat_x=self.WORKITEM_REPEAT_X)
        CL_SOURCE = file(os.path.join(base_path, 'kernels.cl'), 'r').read()
        CL_FLAGS = "-I %s -cl-std=CL1.2 %s" %(common_lib_path, KERNEL_FLAGS)
        CL_FLAGS = cl_opt_decorate(self, CL_FLAGS, self.best_wg_size)
        print '%r compile flags: %s'%(self.__class__.__name__, CL_FLAGS)
        self.program = cl.Program(ctx, CL_SOURCE).build(options=CL_FLAGS)

        self._make_connectivity_image                          = self.program.make_connectivity_image
        self._label_tiles                                      = self.program.label_tiles
        self._compact_paths_global                             = self.program.compact_paths_global
        self._merge_tiles                                      = self.program.merge_tiles
        #self._mark_root_classes                                = self.program.mark_root_classes
        self._relabel_with_scanline_order                      = self.program.relabel_with_scanline_order
        self._mark_roots_and_make_intra_wg_block_local_prefix_sums   = self.program.mark_roots_and_make_intra_wg_block_local_prefix_sums
        self._make_intra_wg_block_global_sums                  = self.program.make_intra_wg_block_global_sums
        self._make_prefix_sums_with_intra_wg_block_global_sums = self.program.make_prefix_sums_with_intra_wg_block_global_sums

    def make_connectivity_image(self, queue, image, wait_for = None):
        tile_dims = self.TILE_COLS, self.TILE_ROWS
        ldims = self.WORKGROUP_TILE_SIZE_X, self.WORKGROUP_TILE_SIZE_Y
        rows, cols = int(self.img_size[0]), int(self.img_size[1])
        r_blocks, c_blocks = divUp(rows, tile_dims[1]), divUp(cols, tile_dims[0])
        gdims = (c_blocks * ldims[0], r_blocks * ldims[1])
        connectivityim = clarray.empty(queue, tuple(self.img_size), uint32)
        event = self._label_tiles(queue,
            gdims, ldims,
            uint32(rows), uint32(cols),
            image.data, uint32(image.strides[0]),
            connectivityim.data, uint32(connectivityim.strides[0]),
            wait_for = wait_for
        )
        return event, connectivityim

    def label_tiles(self, queue, connectivityim, wait_for = None):
        labelim = clarray.empty(queue, tuple(self.img_size), uint32)
        tile_dims = self.TILE_COLS, self.TILE_ROWS
        ldims = self.WORKGROUP_TILE_SIZE_X, self.WORKGROUP_TILE_SIZE_Y
        rows, cols = int(self.img_size[0]), int(self.img_size[1])
        r_blocks, c_blocks = divUp(rows, tile_dims[1]), divUp(cols, tile_dims[0])
        gdims = (c_blocks * ldims[0], r_blocks * ldims[1])
        labelim = clarray.empty(queue, tuple(self.img_size), self.label_dtype)
        event = self._label_tiles(queue,
            gdims, ldims,
            uint32(rows), uint32(cols),
            labelim.data, uint32(labelim.strides[0]),
            connectivityim.data, uint32(connectivityim.strides[0]),
            wait_for = wait_for
        )
        return event, labelim

    def compact_paths(self, queue, labelim, wait_for = None):
        ldims = self.COMPACT_TILE_COLS, self.COMPACT_TILE_ROWS
        rows, cols = int(self.img_size[0]), int(self.img_size[1])
        r_blocks, c_blocks = divUp(rows, ldims[1]), divUp(cols, ldims[0])
        gdims = (c_blocks * ldims[0], r_blocks * ldims[1])
        event = self._label_tiles(queue,
            gdims, ldims,
            uint32(rows), uint32(cols),
            labelim.data, uint32(labelim.strides[0]),
            wait_for = wait_for
        )
        return event,

    def merge_tiles(self, queue, connectivityim, labelim, merge_tiles_rc, merge_tile_size_rc, grid_size_rc, wait_for = None):
        rows, cols = int(self.img_size[0]), int(self.img_size[1])
        ldims = self.WORKGROUP_TILE_SIZE_X, self.WORKGROUP_TILE_SIZE_Y
        merge_grid_rc = divUp(grid_rc[0], merge_tiles_rc[0]), divUp(grid_rc[1], merge_tiles_rc[1])
        gdims = merge_grid_rc[1] * ldims[0], merge_grid_rc[0] * ldims[1]
        event = self._merge_tiles(queue,
            gdims, ldims,
            uint32(rows), uint32(cols),
            uint32(merge_tiles_rc[0]), uint32(merge_tiles_rc[1]),
            uint32(merge_tile_size_rc[0]), uint32(merge_tile_size_rc[1]),
            connectivityim.data, uint32(connectivityim.strides[0]),
            labelim.data, uint32(labelim.strides[0]),
            uint32(grid_size_rc[0] % merge_tiles_rc[0]), uint32(grid_size_rc[1] % merge_tiles_rc[1]),
            wait_for = wait_for
        )
        return event,

    def mark_roots_and_make_prefix_sums(self, queue, image, labelim, wait_for = None):
        compute_units = device.max_compute_units
        wg_size = self.best_wg_size
        n_pixels = self.img_size[0] * self.img_size[1]
        nblocks = divUp(n_pixels, wg_size)
        nblocks_per_wg = nblocks//wgs_per_tile
        n_block_sums = nblocks//nblocks_per_wg
        intra_wg_block_sums = clarray.empty(queue, (n_block_sums,), np.uint32)
        prefix_sums = clarray.empty(queue, tuple(self.img_size), np.uint32)
        gdims = (nblocks * wg_size, )
        ldims = (wg_size,)
        event = self._mark_roots_and_make_wg_block_local_prefix_sums(queue, (nblocks * wg_size,), (wg_size,),
            uint32(im_rows), uint32(im_cols),
            image.data, uint32(image.strides[0]),
            labelim.data, uint32(labelim.strides[0]),
            intra_wg_block_sums.data, uint32(intra_wg_block_sums.strides[0]),
            prefix_sums.data, uint32(prefix_sums.strides[0]),
            wait_for = wait_for
        )
        event = self._make_2D_array_of_intra_wg_block_global_sums(queue, (1 * wg_size,), (wg_size),
            intra_wg_block_sums.data, uint32(intra_wg_block_sums.strides[0]), uint32(n_block_sums),
            wait_for=[event]
        )
        event = self._make_prefix_sums_with_intra_wg_block_global_sums(queue, (nblocks * wg_size,), (wg_size,),
            uint32(rows), uint32(cols),
            intra_wg_block_sums.data, intra_wg_block_sums,
            prefix_sums.data, uint32(prefix_sums.strides[0]),
            wait_for=[event]
        )

        return event, prefix_sums

    def relabel_with_scanline_order(self, queue, image, labelim, label_root_class_psumim, wait_for = None):
        labelim_result = clarray.empty(queue, tuple(self.img_size), self.label_dtype)
        ldims = self.COMPACT_TILE_COLS, self.COMPACT_TILE_ROWS
        rows, cols = int(self.img_size[0]), int(self.img_size[1])
        r_blocks, c_blocks = divUp(rows, ldims[1]), divUp(cols, ldims[0])
        gdims = (c_blocks * ldims[0], r_blocks * ldims[1])
        event = self._relabel_with_scanline_order(queue,
            gdims, ldims,
            uint32(rows), uint32(cols),
            labelim_result.data, uint32(labelim_result.strides[0]),
            image.data, uint32(image.strides[0]),
            labelim.data, uint32(labelim.strides[0]),
            label_root_class_psumim.data, uint32(label_root_class_psumim.strides[0]),
            wait_for = wait_for
        )
        return event, labelim_result

    def __call__(self, queue, cl_img, wait_for = None):
        event, connectivityim = self.make_connectivity_image(queue, cl_img, wait_for=wait_for)
        event, labelim = self.label_tiles(queue, connectivityim, wait_for = [event])

        merge_grid_rc = divUp(self.img_size[0], self.TILE_ROWS), divUp(self.img_size[1], self.TILE_COLS)
        tile_size_rc = self.TILE_ROWS, self.TILE_COLS
        merge_tiles_rc = (2, 2)
        merge_block_dims_cr = self.WORKGROUP_TILE_SIZE_X, self.WORKGROUP_TILE_SIZE_Y
        while merge_grid_rc[0] > 1 or merge_grid_rc[1] > 1:
            event, = self.merge_tiles(queue, connectivityim, labelim, merge_tiles_rc, merge_tile_size_rc, grid_size_rc, wait_for = [event])

            merge_grid_rc = divUp(merge_grid_rc[0], merge_tiles_rc[0]), divUp(merge_grid_rc[1], merge_tiles_rc[1])
            tile_size_rc = tile_size_rc[0] * merge_tiles_rc[0], tile_size_rc[1] * merge_tiles_rc[1]

        event, = self.compact_paths(queue, tiled_labelim, wait_for = [event])
        event, prefix_sums = self.mark_roots_and_make_prefix_sums(queue, image, labelim, wait_for = [event])
        event, labelim_result = self.relabel_with_scanline_order(queue, image, labelim, prefix_sums, wait_for = [event])
        return event, labelim_result
