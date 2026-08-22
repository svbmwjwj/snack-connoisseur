import argparse
import boto3
import json
import uuid
import sys

def create_instance(args=None, **kwargs):
    if args is not None:
        instance_name = getattr(args, 'alias', None)
        region = getattr(args, 'region', None)
        count = getattr(args, 'count', 1)
        bundle_id = getattr(args, 'bundle_id', "nano_3_0")
        blueprint_id = getattr(args, 'blueprint_id', "debian_12")
        user_data = getattr(args, 'user_data', None)
        key_pair_name = getattr(args, 'key_pair_name', None)
    else:
        instance_name = kwargs.get("instance_name")
        region = kwargs.get("region")
        count = kwargs.get("count", 1)
        bundle_id = kwargs.get("bundle_id", "nano_3_0")
        blueprint_id = kwargs.get("blueprint_id", "debian_12")
        user_data = kwargs.get("user_data")
        key_pair_name = kwargs.get("key_pair_name")

    client = boto3.client("lightsail", region_name=region)
    
    # Format instance names based on count
    instance_names = []
    if count > 1:
        instance_names = [f"{instance_name}_{i}" for i in range(1, count + 1)]
    else:
        instance_names = [instance_name]
        
    # Idempotency check using get_instances
    instances_to_create = []
    
    try:
        all_inst_resp = client.get_instances()
        if isinstance(all_inst_resp, dict) and "instances" in all_inst_resp:
            existing_names = {inst.get("name") for inst in all_inst_resp.get("instances", [])}
        else:
            existing_names = set()
    except Exception:
        existing_names = set()
        
    for name in instance_names:
        if name not in existing_names:
            instances_to_create.append(name)
            
    if instances_to_create:
        create_kwargs = {
            "instanceNames": instances_to_create,
            "availabilityZone": f"{region}a",
            "bundleId": bundle_id,
            "blueprintId": blueprint_id,
        }
        
        if user_data:
            create_kwargs["userData"] = user_data
        if key_pair_name:
            create_kwargs["keyPairName"] = key_pair_name
            
        client.create_instances(**create_kwargs)
        
    # Fetch all info
    results = []
    for name in instance_names:
        resp = client.get_instance(instanceName=name)
        results.append({
            "name": name,
            "ip": resp["instance"].get("publicIpAddress", "")
        })
        
    if count == 1:
        return results[0]
    return results


def rotate_ip(args=None, **kwargs):
    if args is not None:
        instance_name = getattr(args, 'alias', None)
        region = getattr(args, 'region', None)
        current_ip = getattr(args, 'current_ip', None)
    else:
        instance_name = kwargs.get("instance_name")
        region = kwargs.get("region")
        current_ip = kwargs.get("current_ip")
        
    client = boto3.client("lightsail", region_name=region)
    
    if not instance_name and current_ip:
        # Find the instance by IP
        resp = client.get_instances()
        if isinstance(resp, dict):
            for inst in resp.get("instances", []):
                if inst.get("publicIpAddress") == current_ip:
                    instance_name = inst.get("name")
                    break
                
    if not instance_name:
        raise ValueError("Could not find instance")
        
    # Check for existing static IP attached to this instance
    existing_ip = None
    existing_static_ip_name = None
    
    ips_resp = client.get_static_ips()
    if isinstance(ips_resp, dict):
        for static_ip in ips_resp.get("staticIps", []):
            if static_ip.get("attachedTo") == instance_name:
                existing_ip = static_ip.get("ipAddress")
                existing_static_ip_name = static_ip.get("name")
                break
            
    if not existing_ip:
        # Check instance public ip
        inst_resp = client.get_instances()
        if isinstance(inst_resp, dict):
            for inst in inst_resp.get("instances", []):
                if inst.get("name") == instance_name:
                    existing_ip = inst.get("publicIpAddress")
                    break
                
    # Allocate new static IP
    new_ip_name = f"ip-auto-{uuid.uuid4().hex[:8]}"
    client.allocate_static_ip(staticIpName=new_ip_name)
    
    new_ip_resp = client.get_static_ip(staticIpName=new_ip_name)
    new_ip_addr = new_ip_resp["staticIp"]["ipAddress"]
    
    client.attach_static_ip(staticIpName=new_ip_name, instanceName=instance_name)
    
    if existing_static_ip_name:
        client.release_static_ip(staticIpName=existing_static_ip_name)
        
    res = {"new_ip": new_ip_addr}
    if existing_ip:
        res["old_ip"] = existing_ip
    if existing_static_ip_name:
        res["old_static_ip"] = existing_static_ip_name
        
    return res

def open_port(args=None, **kwargs):
    if args is not None:
        instance_name = getattr(args, 'alias', None)
        port = getattr(args, 'port', None)
        region = getattr(args, 'region', None)
        protocol = getattr(args, 'protocol', "tcp")
    else:
        instance_name = kwargs.get("instance_name")
        port = kwargs.get("port")
        region = kwargs.get("region")
        protocol = kwargs.get("protocol", "tcp")
        
    client = boto3.client("lightsail", region_name=region)
    
    port_info = {
        "fromPort": int(port),
        "toPort": int(port),
        "protocol": protocol
    }
    
    client.open_instance_port(
        instanceName=instance_name,
        portInfo=port_info
    )
    
    return {"status": "success"}

def main(cli_args=None):
    if cli_args is None:
        cli_args = sys.argv[1:]
        
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    
    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--alias", required=True)
    create_parser.add_argument("--region", required=True)
    create_parser.add_argument("--count", type=int, default=1)
    
    rotate_parser = subparsers.add_parser("rotate-ip")
    rotate_parser.add_argument("--alias", required=True)
    rotate_parser.add_argument("--region", required=True)
    
    port_parser = subparsers.add_parser("open-port")
    port_parser.add_argument("--alias", required=True)
    port_parser.add_argument("--region", required=True)
    port_parser.add_argument("--port", required=True, type=int)
    
    parsed = parser.parse_args(cli_args)
    
    if parsed.command == "create":
        res = create_instance(args=parsed)
        print(json.dumps(res))
        return 0
    elif parsed.command == "rotate-ip":
        res = rotate_ip(args=parsed)
        print(json.dumps(res))
        return 0
    elif parsed.command == "open-port":
        res = open_port(args=parsed)
        print(json.dumps(res))
        return 0
        
    return 1

if __name__ == "__main__":
    sys.exit(main())
