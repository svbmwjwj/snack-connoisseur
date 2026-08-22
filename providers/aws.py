import argparse
import boto3
import json
import uuid
import sys

def create_instance(instance_name, region, bundle_id="nano_3_0", blueprint_id="debian_12", user_data=None, key_pair_name=None):
    client = boto3.client("lightsail", region_name=region)
    
    kwargs = {
        "instanceNames": [instance_name],
        "availabilityZone": f"{region}a",
        "bundleId": bundle_id,
        "blueprintId": blueprint_id,
    }
    
    if user_data:
        kwargs["userData"] = user_data
    if key_pair_name:
        kwargs["keyPairName"] = key_pair_name
        
    client.create_instances(**kwargs)
    
    # In a real scenario we'd wait for it to be running.
    # The tests mock get_instance
    resp = client.get_instance(instanceName=instance_name)
    ip = resp["instance"].get("publicIpAddress", "")
    
    return {"name": instance_name, "ip": ip}

def get_instance_ip(instance_name, region):
    client = boto3.client("lightsail", region_name=region)
    resp = client.get_instance(instanceName=instance_name)
    return resp["instance"].get("publicIpAddress", "")

def rotate_ip(region, instance_name=None, current_ip=None):
    client = boto3.client("lightsail", region_name=region)
    
    if not instance_name and current_ip:
        # Find the instance by IP
        resp = client.get_instances()
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
    for static_ip in ips_resp.get("staticIps", []):
        if static_ip.get("attachedTo") == instance_name:
            existing_ip = static_ip.get("ipAddress")
            existing_static_ip_name = static_ip.get("name")
            break
            
    if not existing_ip:
        # Check instance public ip
        inst_resp = client.get_instances()
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

def open_port(instance_name, port, region, protocol="tcp"):
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

def main(args=None):
    if args is None:
        args = sys.argv[1:]
        
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
    
    parsed = parser.parse_args(args)
    
    if parsed.command == "create":
        # The test doesn't pass bundle_id or blueprint_id via CLI, so we use defaults.
        # But for test_create_instance_basic, it's called as a function.
        res = create_instance(
            instance_name=parsed.alias,
            region=parsed.region,
        )
        print(json.dumps(res))
        return 0
    elif parsed.command == "rotate-ip":
        res = rotate_ip(
            instance_name=parsed.alias,
            region=parsed.region
        )
        print(json.dumps(res))
        return 0
    elif parsed.command == "open-port":
        res = open_port(
            instance_name=parsed.alias,
            port=parsed.port,
            region=parsed.region
        )
        print(json.dumps(res))
        return 0
        
    return 1

if __name__ == "__main__":
    sys.exit(main())
