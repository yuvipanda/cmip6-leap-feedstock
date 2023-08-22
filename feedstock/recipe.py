### needs to be here otherwise the import fails
"""Modified transforms from Pangeo Forge"""

import apache_beam as beam
from dataclasses import dataclass
from datetime import datetime
from typing import List
import json
from pangeo_forge_recipes.patterns import pattern_from_file_sequence
from pangeo_forge_recipes.transforms import (
    OpenURLWithFSSpec, OpenWithXarray, StoreToZarr, Indexed, T
)
import xarray as xr
import zarr

@dataclass
class Preprocessor(beam.PTransform):
    """
    Preprocessor for xarray datasets.
    Set all data_variables except for `variable_id` attrs to coord
    Add additional information 

    """

    @staticmethod
    def _keep_only_variable_id(item: Indexed[T]) -> Indexed[T]:
        """
        Many netcdfs contain variables other than the one specified in the `variable_id` facet. 
        Set them all to coords
        """
        index, ds = item
        print(f"Preprocessing before {ds =}")
        new_coords_vars = [var for var in ds.data_vars if var != ds.attrs['variable_id']]
        ds = ds.set_coords(new_coords_vars)
        print(f"Preprocessing after {ds =}")
        return index, ds
    
    def expand(self, pcoll: beam.PCollection) -> beam.PCollection:
        return ( pcoll 
            | "Fix coordinates" >> beam.Map(self._keep_only_variable_id) 
        )

@dataclass
class TestDataset(beam.PTransform):
    """
    Test stage for data written to zarr store
    """
    iid: str
    
    @staticmethod
    def _get_dataset(store: zarr.storage.FSStore) -> xr.Dataset:
        import xarray as xr
        return xr.open_dataset(store, engine='zarr', chunks={}, use_cftime=True)
       
    def _test_open_store(self, store: zarr.storage.FSStore) -> zarr.storage.FSStore:
        """Can the store be opened?"""
        print(f"Written path: {store.path =}")
        ds = self._get_dataset(store)
        print(ds)
        return store
        
    def _test_time(self, store: zarr.storage.FSStore) -> zarr.storage.FSStore:
        """
        Check time dimension
        For now checks:
        - That time increases strictly monotonically
        - That no large gaps in time (e.g. missing file) are present
        """
        ds = self._get_dataset(store)
        time_diff = ds.time.diff('time').astype(int)
        # assert that time increases monotonically
        print(time_diff)
        assert (time_diff > 0).all()
        
        # assert that there are no large time gaps
        mean_time_diff = time_diff.mean()
        normalized_time_diff = abs((time_diff - mean_time_diff)/mean_time_diff)
        assert (normalized_time_diff<0.05).all()
        return store
    
    def _test_attributes(self, store: zarr.storage.FSStore) -> zarr.storage.FSStore:
        ds = self._get_dataset(store)
        
        # check completeness of attributes 
        iid_schema = "mip_era.activity_id.institution_id.source_id.experiment_id.variant_label.table_id.variable_id.grid_label.version"
        for facet_value, facet in zip(self.iid.split('.'), iid_schema.split('.')):
            if not 'version' in facet: #(TODO: Why is the version not in all datasets?)
                print(f"Checking {facet = } in dataset attributes")
                assert ds.attrs[facet] == facet_value
          
        return store
    
    def expand(self, pcoll: beam.PCollection) -> beam.PCollection:
        return (pcoll
            | "Testing - Open Store" >> beam.Map(self._test_open_store)
            | "Testing - Attributes" >> beam.Map(self._test_attributes)
            | "Testing - Time Dimension" >> beam.Map(self._test_time)
        )


# NOTE: This is a simplified setup, mainly to test the changes to StoreToZarr
# here (https://github.com/pangeo-forge/pangeo-forge-recipes/pull/546). I 
# will start a new PR to try to refactor this as a dict-object, after 
# checking in with Charles (https://github.com/leap-stc/cmip6-leap-feedstock/pull/4#issuecomment-1666929555)

with open('feedstock/tim_batch.json') as json_file:
    url_dict = json.load(json_file)

target_chunks_aspect_ratio = {'time': 1}

recipes = {}
for iid, urls in url_dict.items():
    pattern = pattern_from_file_sequence(
        urls,
        concat_dim='time'
        )
    recipes[iid] = (
        beam.Create(pattern.items())
        | OpenURLWithFSSpec()
        # | OpenWithXarray(xarray_open_kwargs={"use_cftime":True}) # do not specify file type to accomodate both ncdf3 and ncdf4
        | OpenWithXarray()
        | Preprocessor()
        | StoreToZarr(
            store_name=f"{iid}.zarr",
            combine_dims=pattern.combine_dim_keys,
            # target_chunks={'time':400},
            target_chunk_size='150MB',
            target_chunks_aspect_ratio = target_chunks_aspect_ratio,
            size_tolerance=0.3,
            allow_fallback_algo=True,
            )
        | TestDataset(iid=iid)
        )
