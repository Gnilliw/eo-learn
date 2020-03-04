"""
Credits:
Copyright (c) 2017-2020 Matej Aleksandrov, Matej Batič, Grega Milčinski, Matic Lubej, Devis Peresutti (Sinergise)
Copyright (c) 2017-2020 Nejc Vesel, Jovan Višnjić, Anže Zupanc (Sinergise)

This source code is licensed under the MIT license found in the LICENSE
file in the root directory of this source tree.
"""
import unittest
import logging
import datetime
import os
import tempfile

import numpy as np
import fs
from fs.errors import CreateFailed, ResourceNotFound
from fs.tempfs import TempFS
from fs_s3fs import S3FS
from geopandas import GeoDataFrame
from moto import mock_s3
import boto3

from sentinelhub import BBox, CRS
from eolearn.core import EOPatch, FeatureType, OverwritePermission, SaveTask, LoadTask, to_gpd_crs

logging.basicConfig(level=logging.INFO)


@mock_s3
def _create_new_s3_fs():
    """ Creates a new empty mocked s3 bucket. If one such bucket already exists it deletes it first.
    """
    bucket_name = 'mocked-test-bucket'
    s3resource = boto3.resource('s3', region_name='eu-central-1')

    bucket = s3resource.Bucket(bucket_name)

    if bucket.creation_date:  # If bucket already exists
        for key in bucket.objects.all():
            key.delete()
        bucket.delete()

    s3resource.create_bucket(Bucket=bucket_name,
                             CreateBucketConfiguration={'LocationConstraint': 'eu-central-1'})

    return S3FS(bucket_name=bucket_name)


@mock_s3
class TestEOPatchIO(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        eopatch = EOPatch()
        mask = np.zeros((3, 3, 2), dtype=np.int16)
        eopatch.data_timeless['mask'] = mask
        eopatch.timestamp = [datetime.datetime(2017, 1, 1, 10, 4, 7),
                             datetime.datetime(2017, 1, 4, 10, 14, 5)]
        eopatch.meta_info['something'] = 'nothing'
        eopatch.meta_info['something-else'] = 'nothing'
        eopatch.bbox = BBox((1, 2, 3, 4), CRS.WGS84)
        eopatch.scalar['my scalar with spaces'] = np.array([[1, 2, 3]])
        eopatch.vector['my-df'] = GeoDataFrame({
            'values': [1],
            'TIMESTAMP': [datetime.datetime(2017, 1, 1, 10, 4, 7)],
            'geometry': [eopatch.bbox.geometry]
        }, crs=to_gpd_crs(eopatch.bbox.crs))

        cls.eopatch = eopatch

        cls.filesystem_loaders = [TempFS, _create_new_s3_fs]

    def test_saving_to_a_file(self):
        with tempfile.NamedTemporaryFile() as fp:
            with self.assertRaises(CreateFailed):
                self.eopatch.save(fp.name)

    def test_saving_in_empty_folder(self):
        for fs_loader in self.filesystem_loaders:
            with fs_loader() as temp_fs:

                if isinstance(temp_fs, TempFS):
                    self.eopatch.save(temp_fs.root_path)
                else:
                    self.eopatch.save('/', filesystem=temp_fs)
                self.assertTrue(temp_fs.exists('/data_timeless/mask.npy'))

                subfolder = 'new-subfolder'
                self.eopatch.save('new-subfolder', filesystem=temp_fs)
                self.assertTrue(temp_fs.exists('/{}/bbox.pkl'.format(subfolder)))

    def test_saving_in_non_empty_folder(self):
        for fs_loader in self.filesystem_loaders:
            with fs_loader() as temp_fs:
                empty_file = 'foo.txt'

                with temp_fs.open(empty_file, 'w'):
                    pass

                self.eopatch.save('/', filesystem=temp_fs)
                self.assertTrue(temp_fs.exists(empty_file))

                self.eopatch.save('/', overwrite_permission=OverwritePermission.OVERWRITE_PATCH, filesystem=temp_fs)
                self.assertFalse(temp_fs.exists(empty_file))

    def test_overwriting_non_empty_folder(self):
        for fs_loader in self.filesystem_loaders:
            with fs_loader() as temp_fs:
                self.eopatch.save('/', filesystem=temp_fs)
                self.eopatch.save('/', filesystem=temp_fs, overwrite_permission=OverwritePermission.OVERWRITE_FEATURES)
                self.eopatch.save('/', filesystem=temp_fs, overwrite_permission=OverwritePermission.OVERWRITE_PATCH)

                add_eopatch = EOPatch()
                add_eopatch.data['some data'] = np.empty((2, 3, 3, 2))
                add_eopatch.save('/', filesystem=temp_fs, overwrite_permission=OverwritePermission.ADD_ONLY)
                with self.assertRaises(ValueError):
                    add_eopatch.save('/', filesystem=temp_fs, overwrite_permission=OverwritePermission.ADD_ONLY)

                new_eopatch = EOPatch.load('/', filesystem=temp_fs, lazy_loading=False)
                self.assertEqual(new_eopatch, self.eopatch + add_eopatch)

    def test_save_load(self):
        for fs_loader in self.filesystem_loaders:
            with fs_loader() as temp_fs:
                self.eopatch.save('/', filesystem=temp_fs)
                eopatch2 = EOPatch.load('/', filesystem=temp_fs)
                self.assertEqual(self.eopatch, eopatch2)

                eopatch2.save('/', filesystem=temp_fs, overwrite_permission=1)
                eopatch2 = EOPatch.load('/', filesystem=temp_fs)
                self.assertEqual(self.eopatch, eopatch2)

                eopatch2.save('/', filesystem=temp_fs, overwrite_permission=1)
                eopatch2 = EOPatch.load('/', filesystem=temp_fs, lazy_loading=False)
                self.assertEqual(self.eopatch, eopatch2)

                features = {FeatureType.DATA_TIMELESS: {'mask'}, FeatureType.TIMESTAMP: ...}
                eopatch2.save('/', filesystem=temp_fs, features=features,
                              compress_level=3, overwrite_permission=1)
                eopatch2 = EOPatch.load('/', filesystem=temp_fs, lazy_loading=True)
                self.assertEqual(self.eopatch, eopatch2)
                eopatch3 = EOPatch.load('/', filesystem=temp_fs, lazy_loading=True, features=features)
                self.assertNotEqual(self.eopatch, eopatch3)

    def test_save_add_only_features(self):
        features = [
            (FeatureType.DATA_TIMELESS, 'mask'),
            FeatureType.MASK,
            FeatureType.VECTOR,
            (FeatureType.SCALAR, ...),
            (FeatureType.META_INFO, 'something'),
            FeatureType.BBOX
        ]

        for fs_loader in self.filesystem_loaders:
            with fs_loader() as temp_fs:
                self.eopatch.save('/', filesystem=temp_fs, features=features, overwrite_permission=0)

    def test_overwrite_failure(self):
        eopatch = EOPatch()
        mask = np.arange(3 * 3 * 2).reshape(3, 3, 2)
        eopatch.data_timeless['mask'] = mask
        eopatch.data_timeless['Mask'] = mask

        for fs_loader in self.filesystem_loaders:
            with fs_loader() as temp_fs, self.assertRaises(IOError):
                eopatch.save('/', filesystem=temp_fs)

        for fs_loader in self.filesystem_loaders:
            with fs_loader() as temp_fs:
                eopatch.save('/', filesystem=temp_fs, features=[(FeatureType.DATA_TIMELESS, 'mask')],
                             overwrite_permission=2)

                with self.assertRaises(IOError):
                    eopatch.save('/', filesystem=temp_fs, features=[(FeatureType.DATA_TIMELESS, 'Mask')],
                                 overwrite_permission=0)

    def test_save_and_load_tasks(self):
        folder = 'foo-folder'
        patch_folder = 'patch-folder'
        for fs_loader in self.filesystem_loaders:
            with fs_loader() as temp_fs:
                temp_fs.makedir(folder)

                save_task = SaveTask(folder, filesystem=temp_fs, compress_level=9)
                load_task = LoadTask(folder, filesystem=temp_fs, lazy_loading=False)

                saved_eop = save_task(self.eopatch, eopatch_folder=patch_folder)
                bbox_path = fs.path.join(folder, patch_folder, 'bbox.pkl.gz')
                self.assertTrue(temp_fs.exists(bbox_path))
                self.assertEqual(saved_eop, self.eopatch)

                eop = load_task(eopatch_folder=patch_folder)
                self.assertEqual(eop, self.eopatch)

    def test_fail_saving_nonexistent_feature(self):
        features = [(FeatureType.DATA, 'nonexistent')]
        for fs_loader in self.filesystem_loaders:
            with fs_loader() as temp_fs, self.assertRaises(ValueError):
                self.eopatch.save('/', filesystem=temp_fs, features=features)

    def test_fail_loading_nonexistent_feature(self):
        for features in [[(FeatureType.DATA, 'nonexistent')], [(FeatureType.META_INFO, 'nonexistent')]]:
            for fs_loader in self.filesystem_loaders:
                with fs_loader() as temp_fs, self.assertRaises(IOError):
                    EOPatch.load('/', filesystem=temp_fs, features=features)

    def test_nonexistent_location(self):
        path = './folder/subfolder/new-eopatch/'
        empty_eop = EOPatch()

        for fs_loader in self.filesystem_loaders:
            with fs_loader() as temp_fs:
                with self.assertRaises(ResourceNotFound):
                    EOPatch.load(path, filesystem=temp_fs)

                empty_eop.save(path, filesystem=temp_fs)

        with TempFS() as temp_fs:
            full_path = os.path.join(temp_fs.root_path, path)
            with self.assertRaises(CreateFailed):
                EOPatch.load(full_path)

            load_task = LoadTask(full_path)
            with self.assertRaises(CreateFailed):
                load_task.execute()

            empty_eop.save(full_path)
            self.assertTrue(os.path.exists(full_path))

        with TempFS() as temp_fs:
            full_path = os.path.join(temp_fs.root_path, path)
            save_task = SaveTask(full_path)
            save_task.execute(empty_eop)
            self.assertTrue(os.path.exists(full_path))


if __name__ == '__main__':
    unittest.main()
