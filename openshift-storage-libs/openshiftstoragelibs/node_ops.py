import time

from glustolibs.gluster.exceptions import ExecutionError
from glusto.core import Glusto as g

from openshiftstoragelibs.cloundproviders.vmware import VmWare
from openshiftstoragelibs import exceptions
from openshiftstoragelibs import waiter


CLOUD_PROVIDER = None


def node_reboot_by_command(node, timeout=600, wait_step=10):
    """Reboot node and wait to start for given timeout.

    Args:
        node (str)     : Node which needs to be rebooted.
        timeout (int)  : Seconds to wait before node to be started.
        wait_step (int): Interval in seconds to wait before checking
                         status of node again.
    """
    cmd = "sleep 3; /sbin/shutdown -r now 'Reboot triggered by Glusto'"
    ret, out, err = g.run(node, cmd)
    if ret != 255:
        err_msg = "failed to reboot host '%s' error %s" % (node, err)
        g.log.error(err_msg)
        raise AssertionError(err_msg)

    try:
        g.ssh_close_connection(node)
    except Exception as e:
        g.log.error("failed to close connection with host %s "
                    "with error: %s" % (node, e))
        raise

    # added sleep as node will restart after 3 sec
    time.sleep(3)

    for w in waiter.Waiter(timeout=timeout, interval=wait_step):
        try:
            if g.rpyc_get_connection(node, user="root"):
                g.rpyc_close_connection(node, user="root")
                return
        except Exception as err:
            g.log.info("exception while getting connection: '%s'" % err)

    if w.expired:
        error_msg = ("exceeded timeout %s sec, node '%s' is "
                     "not reachable" % (timeout, node))
        g.log.error(error_msg)
        raise exceptions.ExecutionError(error_msg)


def wait_for_ssh_connection(hostname, timeout=600, interval=10):
    """Wait for ssh conection to be ready within given timeout.

    Args:
        hostname (str): hostname of a machine.
    Returns:
        None
    Raises:
        CloudProviderError: In case of any failures.
    """
    for w in waiter.Waiter(timeout, interval):
        try:
            # Run random command to verify ssh connection
            g.run(hostname, 'ls')
            return
        except (exceptions.ExecutionError, ExecutionError):
            g.log.info("Waiting for ssh connection on host '%s'" % hostname)

    msg = 'Not able to connect with the %s' % hostname
    g.log.error(msg)
    raise exceptions.CloudProviderError(msg)


def _get_cloud_provider():
    """Gather cloud provider facts"""

    global CLOUD_PROVIDER
    if CLOUD_PROVIDER:
        return CLOUD_PROVIDER

    try:
        cloud_provider_name = g.config['cloud_provider']['name']
    except KeyError:
        msg = "Incorrect config file. Cloud provider name is missing."
        g.log.error(msg)
        raise exceptions.ConfigError(msg)

    if cloud_provider_name == 'vmware':
        CLOUD_PROVIDER = VmWare()
    else:
        msg = "Cloud Provider %s is not supported." % cloud_provider_name
        g.log.error(msg)
        raise NotImplementedError(msg)

    return CLOUD_PROVIDER


def find_vm_name_by_ip_or_hostname(ip_or_hostname):
    """Find VM name from the ip or hostname.

    Args:
        ip_or_hostname (str): IP address or hostname of VM.
    Returns:
        str: Name of the VM.
    """
    cloudProvider = _get_cloud_provider()
    g.log.info('getting the name of vm for ip or hostname %s' % ip_or_hostname)
    return cloudProvider.find_vm_name_by_ip_or_hostname(ip_or_hostname)


def get_power_state_of_vm_by_name(name):
    """Get the power state of VM.

    Args:
        name (str): name of the VM for which state has to be find.
    Returns:
        str: Power state of the VM.
    """
    cloudProvider = _get_cloud_provider()
    g.log.info('getting the power state of vm "%s"' % name)
    return cloudProvider.get_power_state_of_vm_by_name(name)


def power_off_vm_by_name(name):
    """Power off the virtual machine.

    Args:
        name (str): name of the VM which needs to be powered off.
    Returns:
        None
    """
    cloudProvider = _get_cloud_provider()
    g.log.info('powering off the vm "%s"' % name)
    cloudProvider.power_off_vm_by_name(name)
    g.log.info('powered off the vm "%s" successfully' % name)


def power_on_vm_by_name(name, timeout=600, interval=10):
    """Power on the virtual machine and wait for SSH ready within given
    timeout.

    Args:
        name (str): name of the VM which needs to be powered on.
    Returns:
        None
    Raises:
        CloudProviderError: In case of any failures.
    """
    cloudProvider = _get_cloud_provider()
    g.log.info('powering on the VM "%s"' % name)
    cloudProvider.power_on_vm_by_name(name)
    g.log.info('Powered on the VM "%s" successfully' % name)

    # Wait for hostname to get assigned
    _waiter = waiter.Waiter(timeout, interval)
    for w in _waiter:
        try:
            hostname = cloudProvider.wait_for_hostname(name, 1, 1)
            break
        except Exception as e:
            g.log.info(e)
    if w.expired:
        raise exceptions.CloudProviderError(e)

    # Wait for hostname to ssh connection ready
    for w in _waiter:
        try:
            wait_for_ssh_connection(hostname, 1, 1)
            break
        except Exception as e:
            g.log.info(e)
    if w.expired:
        raise exceptions.CloudProviderError(e)