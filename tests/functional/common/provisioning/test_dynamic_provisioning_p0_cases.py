import time
from unittest import skip

from cnslibs.cns.cns_baseclass import CnsBaseClass
from cnslibs.common.exceptions import ExecutionError
from cnslibs.common.heketi_ops import (
    verify_volume_name_prefix)
from cnslibs.common.openshift_ops import (
    get_gluster_pod_names_by_pvc_name,
    get_pvc_status,
    get_pod_name_from_dc,
    oc_create_secret,
    oc_create_sc,
    oc_create_pvc,
    oc_create_app_dc_with_io,
    oc_create_tiny_pod_with_volume,
    oc_delete,
    oc_rsh,
    scale_dc_pod_amount_and_wait,
    verify_pvc_status_is_bound,
    wait_for_pod_be_ready,
    wait_for_resource_absence)
from cnslibs.common.waiter import Waiter
from glusto.core import Glusto as g


class TestDynamicProvisioningP0(CnsBaseClass):
    '''
     Class that contain P0 dynamic provisioning test cases for
     glusterfile volume
    '''

    def setUp(self):
        super(TestDynamicProvisioningP0, self).setUp()
        self.node = self.ocp_master_node[0]
        self.sc = self.cns_storage_class['storage_class1']

    def _create_storage_class(self, create_name_prefix=False):
        sc = self.cns_storage_class['storage_class1']
        secret = self.cns_secret['secret1']

        # Create secret file for usage in storage class
        self.secret_name = oc_create_secret(
            self.node, namespace=secret['namespace'],
            data_key=self.heketi_cli_key, secret_type=secret['type'])
        self.addCleanup(
            oc_delete, self.node, 'secret', self.secret_name)

        # Create storage class
        self.sc_name = oc_create_sc(
            self.node,
            resturl=sc['resturl'],
            restuser=sc['restuser'], secretnamespace=sc['secretnamespace'],
            secretname=self.secret_name,
            **({"volumenameprefix": sc['volumenameprefix']}
               if create_name_prefix else {})
        )
        self.addCleanup(oc_delete, self.node, 'sc', self.sc_name)

    def _create_and_wait_for_pvcs(self, pvc_size=1,
                                  pvc_name_prefix='autotests-pvc',
                                  pvc_amount=1):
        # Create PVCs
        pvc_names = []
        for i in range(pvc_amount):
            pvc_name = oc_create_pvc(
                self.node, self.sc_name, pvc_name_prefix=pvc_name_prefix,
                pvc_size=pvc_size)
            pvc_names.append(pvc_name)
            self.addCleanup(
                wait_for_resource_absence, self.node, 'pvc', pvc_name)
        for pvc_name in pvc_names:
            self.addCleanup(oc_delete, self.node, 'pvc', pvc_name,
                            raise_on_absence=False)

        # Wait for PVCs to be in bound state
        for pvc_name in pvc_names:
            verify_pvc_status_is_bound(self.node, pvc_name)

        return pvc_names

    def _create_and_wait_for_pvc(self, pvc_size=1,
                                 pvc_name_prefix='autotests-pvc'):
        self.pvc_name = self._create_and_wait_for_pvcs(
            pvc_size=pvc_size, pvc_name_prefix=pvc_name_prefix)[0]
        return self.pvc_name

    def dynamic_provisioning_glusterfile(self, heketi_volname_prefix=False):
        # Create secret and storage class
        self._create_storage_class(heketi_volname_prefix)

        # Create PVC
        pvc_name = self._create_and_wait_for_pvc()

        # Create DC with POD and attached PVC to it.
        dc_name = oc_create_app_dc_with_io(self.node, pvc_name)
        self.addCleanup(oc_delete, self.node, 'dc', dc_name)
        self.addCleanup(scale_dc_pod_amount_and_wait, self.node, dc_name, 0)

        pod_name = get_pod_name_from_dc(self.node, dc_name)
        wait_for_pod_be_ready(self.node, pod_name)

        # Verify Heketi volume name for prefix presence if provided
        if heketi_volname_prefix:
            ret = verify_volume_name_prefix(self.node,
                                            self.sc['volumenameprefix'],
                                            self.sc['secretnamespace'],
                                            pvc_name, self.sc['resturl'])
            self.assertTrue(ret, "verify volnameprefix failed")

        # Make sure we are able to work with files on the mounted volume
        filepath = "/mnt/file_for_testing_io.log"
        for cmd in ("dd if=/dev/urandom of=%s bs=1K count=100",
                    "ls -lrt %s",
                    "rm -rf %s"):
            cmd = cmd % filepath
            ret, out, err = oc_rsh(self.node, pod_name, cmd)
            self.assertEqual(
                ret, 0,
                "Failed to execute '%s' command on %s" % (cmd, self.node))

    def test_dynamic_provisioning_glusterfile(self):
        g.log.info("test_dynamic_provisioning_glusterfile")
        self.dynamic_provisioning_glusterfile(False)

    def test_dynamic_provisioning_glusterfile_volname_prefix(self):
        g.log.info("test_dynamic_provisioning_glusterfile volname prefix")
        self.dynamic_provisioning_glusterfile(True)

    def test_dynamic_provisioning_glusterfile_heketipod_failure(self):
        mount_path = "/mnt"
        datafile_path = '%s/fake_file_for_%s' % (mount_path, self.id())

        # Create secret and storage class
        self._create_storage_class()

        # Create PVC
        app_1_pvc_name = self._create_and_wait_for_pvc()

        # Create app POD with attached volume
        app_1_pod_name = oc_create_tiny_pod_with_volume(
            self.node, app_1_pvc_name, "test-pvc-mount-on-app-pod",
            mount_path=mount_path)
        self.addCleanup(
            wait_for_resource_absence, self.node, 'pod', app_1_pod_name)
        self.addCleanup(oc_delete, self.node, 'pod', app_1_pod_name)

        # Wait for app POD be up and running
        wait_for_pod_be_ready(
            self.node, app_1_pod_name, timeout=60, wait_step=2)

        # Write data to the app POD
        write_data_cmd = (
            "dd if=/dev/urandom of=%s bs=1K count=100" % datafile_path)
        ret, out, err = oc_rsh(self.node, app_1_pod_name, write_data_cmd)
        self.assertEqual(
            ret, 0,
            "Failed to execute command %s on %s" % (write_data_cmd, self.node))

        # Remove Heketi pod
        heketi_down_cmd = "oc scale --replicas=0 dc/%s --namespace %s" % (
            self.heketi_dc_name, self.cns_project_name)
        heketi_up_cmd = "oc scale --replicas=1 dc/%s --namespace %s" % (
            self.heketi_dc_name, self.cns_project_name)
        self.addCleanup(self.cmd_run, heketi_up_cmd)
        heketi_pod_name = get_pod_name_from_dc(
            self.node, self.heketi_dc_name, timeout=10, wait_step=3)
        self.cmd_run(heketi_down_cmd)
        wait_for_resource_absence(self.node, 'pod', heketi_pod_name)

        # Create second PVC
        app_2_pvc_name = oc_create_pvc(
            self.node, self.sc_name, pvc_name_prefix='autotests-pvc',
            pvc_size=1)
        self.addCleanup(
            wait_for_resource_absence, self.node, 'pvc', app_2_pvc_name)
        self.addCleanup(oc_delete, self.node, 'pvc', app_2_pvc_name)

        # Check status of the second PVC after small pause
        time.sleep(2)
        ret, status = get_pvc_status(self.node, app_2_pvc_name)
        self.assertTrue(ret, "Failed to get pvc status of %s" % app_2_pvc_name)
        self.assertEqual(
            status, "Pending",
            "PVC status of %s is not in Pending state" % app_2_pvc_name)

        # Create second app POD
        app_2_pod_name = oc_create_tiny_pod_with_volume(
            self.node, app_2_pvc_name, "test-pvc-mount-on-app-pod",
            mount_path=mount_path)
        self.addCleanup(
            wait_for_resource_absence, self.node, 'pod', app_2_pod_name)
        self.addCleanup(oc_delete, self.node, 'pod', app_2_pod_name)

        # Bring Heketi POD back
        self.cmd_run(heketi_up_cmd)

        # Wait for Heketi POD be up and running
        new_heketi_pod_name = get_pod_name_from_dc(
            self.node, self.heketi_dc_name, timeout=10, wait_step=2)
        wait_for_pod_be_ready(
            self.node, new_heketi_pod_name, wait_step=5, timeout=120)

        # Wait for second PVC and app POD be ready
        verify_pvc_status_is_bound(self.node, app_2_pvc_name)
        wait_for_pod_be_ready(
            self.node, app_2_pod_name, timeout=60, wait_step=2)

        # Verify that we are able to write data
        ret, out, err = oc_rsh(self.node, app_2_pod_name, write_data_cmd)
        self.assertEqual(
            ret, 0,
            "Failed to execute command %s on %s" % (write_data_cmd, self.node))

    @skip("Blocked by BZ-1632873")
    def test_dynamic_provisioning_glusterfile_glusterpod_failure(self):
        mount_path = "/mnt"
        datafile_path = '%s/fake_file_for_%s' % (mount_path, self.id())

        # Create secret and storage class
        self._create_storage_class()

        # Create PVC
        pvc_name = self._create_and_wait_for_pvc()

        # Create app POD with attached volume
        pod_name = oc_create_tiny_pod_with_volume(
            self.node, pvc_name, "test-pvc-mount-on-app-pod",
            mount_path=mount_path)
        self.addCleanup(
            wait_for_resource_absence, self.node, 'pod', pod_name)
        self.addCleanup(oc_delete, self.node, 'pod', pod_name)

        # Wait for app POD be up and running
        wait_for_pod_be_ready(
            self.node, pod_name, timeout=60, wait_step=2)

        # Run IO in background
        io_cmd = "oc rsh %s dd if=/dev/urandom of=%s bs=1000K count=900" % (
            pod_name, datafile_path)
        async_io = g.run_async(self.node, io_cmd, "root")

        # Pick up one of the hosts which stores PV brick (4+ nodes case)
        gluster_pod_data = get_gluster_pod_names_by_pvc_name(
            self.node, pvc_name)[0]

        # Delete glusterfs POD from chosen host and wait for spawn of new one
        oc_delete(self.node, 'pod', gluster_pod_data["pod_name"])
        cmd = ("oc get pods -o wide | grep glusterfs | grep %s | "
               "grep -v Terminating | awk '{print $1}'") % (
                   gluster_pod_data["host_name"])
        for w in Waiter(600, 30):
            out = self.cmd_run(cmd)
            new_gluster_pod_name = out.strip().split("\n")[0].strip()
            if not new_gluster_pod_name:
                continue
            else:
                break
        if w.expired:
            error_msg = "exceeded timeout, new gluster pod not created"
            g.log.error(error_msg)
            raise ExecutionError(error_msg)
        new_gluster_pod_name = out.strip().split("\n")[0].strip()
        g.log.info("new gluster pod name is %s" % new_gluster_pod_name)
        wait_for_pod_be_ready(self.node, new_gluster_pod_name)

        # Check that async IO was not interrupted
        ret, out, err = async_io.async_communicate()
        self.assertEqual(ret, 0, "IO %s failed on %s" % (io_cmd, self.node))

    def test_storage_class_mandatory_params_glusterfile(self):
        # CNS-442 storage-class mandatory parameters
        sc = self.cns_storage_class['storage_class1']
        secret = self.cns_secret['secret1']
        node = self.ocp_master_node[0]
        # create secret
        self.secret_name = oc_create_secret(
            node,
            namespace=secret['namespace'],
            data_key=self.heketi_cli_key,
            secret_type=secret['type'])
        self.addCleanup(
            oc_delete, node, 'secret', self.secret_name)

        # create storage class with mandatory parameters only
        self.sc_name = oc_create_sc(
            node, provisioner='kubernetes.io/glusterfs',
            resturl=sc['resturl'], restuser=sc['restuser'],
            secretnamespace=sc['secretnamespace'],
            secretname=self.secret_name
        )
        self.addCleanup(oc_delete, node, 'sc', self.sc_name)

        # Create PVC
        pvc_name = oc_create_pvc(node, self.sc_name)
        self.addCleanup(wait_for_resource_absence, node, 'pvc', pvc_name)
        self.addCleanup(oc_delete, node, 'pvc', pvc_name)
        verify_pvc_status_is_bound(node, pvc_name)

        # Create DC with POD and attached PVC to it.
        dc_name = oc_create_app_dc_with_io(node, pvc_name)
        self.addCleanup(oc_delete, node, 'dc', dc_name)
        self.addCleanup(scale_dc_pod_amount_and_wait, node, dc_name, 0)

        pod_name = get_pod_name_from_dc(node, dc_name)
        wait_for_pod_be_ready(node, pod_name)

        # Make sure we are able to work with files on the mounted volume
        filepath = "/mnt/file_for_testing_sc.log"
        cmd = "dd if=/dev/urandom of=%s bs=1K count=100" % filepath
        ret, out, err = oc_rsh(node, pod_name, cmd)
        self.assertEqual(
            ret, 0, "Failed to execute command %s on %s" % (cmd, node))

        cmd = "ls -lrt %s" % filepath
        ret, out, err = oc_rsh(node, pod_name, cmd)
        self.assertEqual(
            ret, 0, "Failed to execute command %s on %s" % (cmd, node))

        cmd = "rm -rf %s" % filepath
        ret, out, err = oc_rsh(node, pod_name, cmd)
        self.assertEqual(
            ret, 0, "Failed to execute command %s on %s" % (cmd, node))

    def test_dynamic_provisioning_glusterfile_heketidown_pvc_delete(self):
        """ Delete PVC's when heketi is down CNS-438 """

        # Create storage class and secret objects
        self._create_storage_class()

        self.pvc_name_list = self._create_and_wait_for_pvcs(
            1, 'pvc-heketi-down', 3)

        # remove heketi-pod
        scale_dc_pod_amount_and_wait(self.ocp_client[0],
                                     self.heketi_dc_name,
                                     0,
                                     self.cns_project_name)
        try:
            # delete pvc
            for pvc in self.pvc_name_list:
                oc_delete(self.ocp_client[0], 'pvc', pvc)
            for pvc in self.pvc_name_list:
                with self.assertRaises(ExecutionError):
                    wait_for_resource_absence(
                       self.ocp_client[0], 'pvc', pvc,
                       interval=3, timeout=30)
        finally:
            # bring back heketi-pod
            scale_dc_pod_amount_and_wait(self.ocp_client[0],
                                         self.heketi_dc_name,
                                         1,
                                         self.cns_project_name)

        # verify PVC's are deleted
        for pvc in self.pvc_name_list:
            wait_for_resource_absence(self.ocp_client[0], 'pvc',
                                      pvc,
                                      interval=1, timeout=120)

        # create a new PVC
        self._create_and_wait_for_pvc()
