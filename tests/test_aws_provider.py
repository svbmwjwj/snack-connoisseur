import pytest
from unittest.mock import patch, MagicMock
import sys
import os

# Add repository root to sys.path so providers package is discoverable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestAwsProviderCreateInstance:
    """Tests for AWS Lightsail instance creation."""

    @patch("providers.aws.boto3.client")
    def test_create_instance_basic(self, mock_boto):
        from providers.aws import create_instance

        mock_client = MagicMock()
        mock_boto.return_value = mock_client
        mock_client.create_instances.return_value = {
            "operations": [{"status": "Started"}]
        }
        mock_client.get_instance.return_value = {
            "instance": {
                "name": "snack-aws-1",
                "state": {"name": "running"},
                "publicIpAddress": "198.51.100.10",
            }
        }

        result = create_instance(
            instance_name="snack-aws-1",
            region="ap-northeast-1",
            bundle_id="nano_3_0",
            blueprint_id="debian_12",
        )

        mock_boto.assert_called_with("lightsail", region_name="ap-northeast-1")
        mock_client.create_instances.assert_called_once()
        call_kwargs = mock_client.create_instances.call_args[1]
        assert call_kwargs["instanceNames"] == ["snack-aws-1"]
        assert call_kwargs["bundleId"] == "nano_3_0"
        assert call_kwargs["blueprintId"] == "debian_12"
        assert result["ip"] == "198.51.100.10"
        assert result["name"] == "snack-aws-1"

    @patch("providers.aws.boto3.client")
    def test_create_instance_with_user_data_and_key(self, mock_boto):
        from providers.aws import create_instance

        mock_client = MagicMock()
        mock_boto.return_value = mock_client
        mock_client.create_instances.return_value = {"operations": [{"status": "Started"}]}
        mock_client.get_instance.return_value = {
            "instance": {
                "name": "snack-aws-custom",
                "state": {"name": "running"},
                "publicIpAddress": "198.51.100.25",
            }
        }

        user_data_script = "#!/bin/bash\necho 'ssh-ed25519 AAAAC3...' >> /root/.ssh/authorized_keys"
        result = create_instance(
            instance_name="snack-aws-custom",
            region="us-west-2",
            bundle_id="micro_3_0",
            blueprint_id="ubuntu_22_04",
            user_data=user_data_script,
            key_pair_name="my-keypair",
        )

        call_kwargs = mock_client.create_instances.call_args[1]
        assert call_kwargs["userData"] == user_data_script
        assert call_kwargs["keyPairName"] == "my-keypair"
        assert result["ip"] == "198.51.100.25"

    def test_select_az_order(self):
        from providers.aws import select_az_order
        azs = ["ap-northeast-1a", "ap-northeast-1c", "ap-northeast-1d"]
        
        assert select_az_order(azs, "jp_aws-lightsail-1") == ["ap-northeast-1c", "ap-northeast-1d", "ap-northeast-1a"]
        assert select_az_order(azs, "jp_aws-lightsail-2") == ["ap-northeast-1d", "ap-northeast-1a", "ap-northeast-1c"]
        assert select_az_order(azs, "jp_aws-lightsail-3") == ["ap-northeast-1a", "ap-northeast-1c", "ap-northeast-1d"]
        assert select_az_order(azs, "jp_aws-lightsail-4") == ["ap-northeast-1c", "ap-northeast-1d", "ap-northeast-1a"]

    @patch("providers.aws.boto3.client")
    def test_create_instance_auto_az_failover(self, mock_boto):
        from providers.aws import create_instance

        mock_client = MagicMock()
        mock_boto.return_value = mock_client
        mock_client.get_regions.return_value = {
            "regions": [{
                "name": "ap-northeast-1",
                "availabilityZones": [
                    {"zoneName": "ap-northeast-1a", "state": "available"},
                    {"zoneName": "ap-northeast-1c", "state": "available"},
                ]
            }]
        }
        # First AZ fails due to insufficient capacity, second AZ succeeds
        mock_client.create_instances.side_effect = [
            Exception("InsufficientCapacity: Requested bundle is not available in zone"),
            {"operations": [{"status": "Started"}]}
        ]
        mock_client.get_instance.return_value = {
            "instance": {
                "name": "node-2",
                "state": {"name": "running"},
                "publicIpAddress": "198.51.100.33",
            }
        }

        res = create_instance(instance_name="node-2", region="ap-northeast-1")
        assert res["name"] == "node-2"
        assert mock_client.create_instances.call_count == 2
        # First call used zone 2 % 2 = 0 ('ap-northeast-1a'), second call used 'ap-northeast-1c'
        first_call_az = mock_client.create_instances.call_args_list[0][1]["availabilityZone"]
        second_call_az = mock_client.create_instances.call_args_list[1][1]["availabilityZone"]
        assert first_call_az == "ap-northeast-1a"
        assert second_call_az == "ap-northeast-1c"


class TestAwsProviderRotateIp:
    """Tests for AWS Lightsail static IP rotation (断臂求生)."""

    @patch("providers.aws.boto3.client")
    def test_rotate_ip_with_existing_static_ip(self, mock_boto):
        from providers.aws import rotate_ip

        mock_client = MagicMock()
        mock_boto.return_value = mock_client

        # Mock existing instance and attached static IP
        mock_client.get_instances.return_value = {
            "instances": [
                {"name": "snack-aws-1", "publicIpAddress": "198.51.100.10"}
            ]
        }
        mock_client.get_static_ips.return_value = {
            "staticIps": [
                {"name": "ip-old-12345", "ipAddress": "198.51.100.10", "attachedTo": "snack-aws-1"}
            ]
        }
        mock_client.allocate_static_ip.return_value = {"operations": [{"status": "Started"}]}
        mock_client.get_static_ip.return_value = {
            "staticIp": {"name": "ip-auto-99999", "ipAddress": "203.0.113.50"}
        }
        mock_client.attach_static_ip.return_value = {"operations": [{"status": "Started"}]}
        mock_client.release_static_ip.return_value = {"operations": [{"status": "Started"}]}

        result = rotate_ip(instance_name="snack-aws-1", region="ap-northeast-1")

        mock_boto.assert_called_with("lightsail", region_name="ap-northeast-1")
        mock_client.allocate_static_ip.assert_called_once()
        mock_client.attach_static_ip.assert_called_once()
        # Verify old static IP was released
        mock_client.release_static_ip.assert_called_once_with(
            staticIpName="ip-old-12345"
        )
        assert result["new_ip"] == "203.0.113.50"
        assert result.get("old_ip") == "198.51.100.10" or result.get("old_static_ip") == "ip-old-12345"

    @patch("providers.aws.boto3.client")
    def test_rotate_ip_without_prior_static_ip(self, mock_boto):
        from providers.aws import rotate_ip

        mock_client = MagicMock()
        mock_boto.return_value = mock_client

        mock_client.get_instances.return_value = {
            "instances": [
                {"name": "snack-aws-1", "publicIpAddress": "198.51.100.10"}
            ]
        }
        # No attached static IP
        mock_client.get_static_ips.return_value = {"staticIps": []}
        mock_client.allocate_static_ip.return_value = {"operations": [{"status": "Started"}]}
        mock_client.get_static_ip.return_value = {
            "staticIp": {"name": "ip-auto-99999", "ipAddress": "203.0.113.60"}
        }
        mock_client.attach_static_ip.return_value = {"operations": [{"status": "Started"}]}

        result = rotate_ip(instance_name="snack-aws-1", region="ap-northeast-1")

        mock_client.allocate_static_ip.assert_called_once()
        mock_client.attach_static_ip.assert_called_once()
        # release_static_ip should not be called since there was no old static IP
        mock_client.release_static_ip.assert_not_called()
        assert result["new_ip"] == "203.0.113.60"

    @patch("providers.aws.boto3.client")
    def test_rotate_ip_by_current_ip_lookup(self, mock_boto):
        from providers.aws import rotate_ip

        mock_client = MagicMock()
        mock_boto.return_value = mock_client

        mock_client.get_instances.return_value = {
            "instances": [
                {"name": "snack-aws-discovered", "publicIpAddress": "198.51.100.99"}
            ]
        }
        mock_client.get_static_ips.return_value = {"staticIps": []}
        mock_client.allocate_static_ip.return_value = {"operations": [{"status": "Started"}]}
        mock_client.get_static_ip.return_value = {
            "staticIp": {"name": "ip-auto-88888", "ipAddress": "203.0.113.77"}
        }
        mock_client.attach_static_ip.return_value = {"operations": [{"status": "Started"}]}

        result = rotate_ip(current_ip="198.51.100.99", region="ap-northeast-1")

        mock_client.attach_static_ip.assert_called_once()
        call_kwargs = mock_client.attach_static_ip.call_args[1]
        assert call_kwargs["instanceName"] == "snack-aws-discovered"
        assert result["new_ip"] == "203.0.113.77"


class TestAwsProviderOpenPort:
    """Tests for opening cloud firewall ports via Lightsail API."""

    @patch("providers.aws.boto3.client")
    def test_open_port_tcp(self, mock_boto):
        from providers.aws import open_port

        mock_client = MagicMock()
        mock_boto.return_value = mock_client
        mock_client.open_instance_port.return_value = {"operations": [{"status": "Started"}]}

        res = open_port(
            instance_name="snack-aws-1",
            port=2222,
            region="ap-northeast-1",
            protocol="tcp",
        )

        mock_boto.assert_called_with("lightsail", region_name="ap-northeast-1")
        mock_client.open_instance_port.assert_called_once_with(
            instanceName="snack-aws-1",
            portInfo={"fromPort": 2222, "toPort": 2222, "protocol": "tcp"},
        )
        assert res is True or (isinstance(res, dict) and res.get("status") == "success")




class TestAwsProviderDeleteInstance:
    """Tests for AWS Lightsail instance deletion."""

    @patch("providers.aws.boto3.client")
    def test_delete_instance_exact_match(self, mock_boto):
        from providers.aws import delete_instance

        mock_client = MagicMock()
        mock_boto.return_value = mock_client
        mock_client.get_instances.return_value = {
            "instances": [{"name": "jp-node-1"}]
        }
        mock_client.get_static_ips.return_value = {
            "staticIps": [{"name": "static-ip-1", "attachedTo": "jp-node-1"}]
        }

        res = delete_instance(alias="jp-node-1", region="ap-northeast-1")

        mock_boto.assert_called_with("lightsail", region_name="ap-northeast-1")
        mock_client.release_static_ip.assert_called_once_with(staticIpName="static-ip-1")
        mock_client.delete_instance.assert_called_once_with(instanceName="jp-node-1")
        assert res["count"] == 1
        assert res["deleted"][0]["name"] == "jp-node-1"

    @patch("providers.aws.boto3.client")
    def test_delete_instance_wildcard_pattern(self, mock_boto):
        from providers.aws import delete_instance

        mock_client = MagicMock()
        mock_boto.return_value = mock_client
        mock_client.get_instances.return_value = {
            "instances": [
                {"name": "jp-node-1"},
                {"name": "jp-node-2"},
                {"name": "us-node-1"},
            ]
        }
        mock_client.get_static_ips.return_value = {"staticIps": []}

        res = delete_instance(alias="jp-node-*", region="ap-northeast-1")

        assert res["count"] == 2
        assert mock_client.delete_instance.call_count == 2


class TestAwsProviderCli:
    """Tests for the CLI dispatcher entrypoint of providers/aws.py."""

    @patch("providers.aws.create_instance")
    def test_cli_create_instance(self, mock_create, capsys):
        from providers.aws import main

        mock_create.return_value = {"name": "aws-node-1", "ip": "198.51.100.20"}
        exit_code = main(["create", "--alias", "aws-node-1", "--region", "ap-northeast-1", "--bundle", "micro_3_0", "--blueprint", "ubuntu_22_04"])

        assert exit_code == 0
        mock_create.assert_called_once()
        captured = capsys.readouterr()
        assert "198.51.100.20" in captured.out

    @patch("providers.aws.delete_instance")
    def test_cli_delete_instance(self, mock_delete, capsys):
        from providers.aws import main

        mock_delete.return_value = {"count": 1, "deleted": [{"name": "aws-node-1"}]}
        exit_code = main(["delete", "--alias", "aws-node-1", "--region", "ap-northeast-1"])

        assert exit_code == 0
        mock_delete.assert_called_once()
        captured = capsys.readouterr()
        assert "aws-node-1" in captured.out

    @patch("providers.aws.rotate_ip")
    def test_cli_rotate_ip(self, mock_rotate, capsys):
        from providers.aws import main

        mock_rotate.return_value = {"new_ip": "203.0.113.90"}
        exit_code = main(["rotate-ip", "--alias", "aws-node-1", "--region", "ap-northeast-1"])

        assert exit_code == 0
        mock_rotate.assert_called_once()
        captured = capsys.readouterr()
        assert "203.0.113.90" in captured.out

    @patch("providers.aws.open_port")
    def test_cli_open_port(self, mock_open):
        from providers.aws import main

        mock_open.return_value = True
        exit_code = main(["open-port", "--alias", "aws-node-1", "--region", "ap-northeast-1", "--port", "2222"])

        assert exit_code == 0
        mock_open.assert_called_once()

