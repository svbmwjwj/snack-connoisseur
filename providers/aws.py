import argparse
import boto3
import fnmatch
import json
import uuid
import sys
import re

def validate_region(region):
    if not region:
        raise ValueError("Missing AWS region parameter (e.g., --region ap-northeast-1)")
    region = str(region).strip()
    m = re.match(r'^([a-z]+-[a-z]+-\d+)([a-z])$', region)
    if m:
        suggested_region = m.group(1)
        suggested_zone = region
        raise ValueError(
            f"Invalid AWS region '{region}': this is an Availability Zone.\n"
            f"Please set REGION='{suggested_region}' and optionally ZONE='{suggested_zone}' in your profile template."
        )
    return region


def get_healthy_azs(client, region):
    """Query Lightsail for available and healthy AZs in the given region."""
    try:
        resp = client.get_regions(includeAvailabilityZones=True)
        if isinstance(resp, dict):
            for r in resp.get("regions", []):
                if isinstance(r, dict) and r.get("name") == region:
                    azs = [
                        z["zoneName"]
                        for z in r.get("availabilityZones", [])
                        if isinstance(z, dict) and z.get("state") == "available"
                    ]
                    if azs:
                        return sorted(azs)
    except Exception:
        pass
    # Fallback to standard 1a if discovery fails
    return [f"{region}a"]


def select_az_order(healthy_azs, instance_name):
    """
    Given a list of healthy AZs and an instance name, return candidate AZs
    ordered by round-robin selection (based on trailing number or name hash),
    with subsequent AZs available for failover retry.
    """
    if not healthy_azs:
        return []
    if len(healthy_azs) == 1:
        return list(healthy_azs)

    # Extract trailing number if present (e.g. jp_aws-lightsail-1 -> 1, jp-2 -> 2)
    m = re.search(r'[-_](\d+)$', str(instance_name))
    if m:
        idx = int(m.group(1)) % len(healthy_azs)
    else:
        idx = abs(hash(str(instance_name))) % len(healthy_azs)

    return healthy_azs[idx:] + healthy_azs[:idx]


def create_instance(args=None, **kwargs):
    if args is not None:
        instance_name = getattr(args, 'alias', None)
        region = getattr(args, 'region', None)
        zone = getattr(args, 'zone', None)
        count = getattr(args, 'count', 1)
        bundle_id = getattr(args, 'bundle', getattr(args, 'bundle_id', "nano_3_0"))
        blueprint_id = getattr(args, 'blueprint', getattr(args, 'blueprint_id', "debian_12"))
        user_data = getattr(args, 'user_data', None)
        key_pair_name = getattr(args, 'key_pair_name', None)
    else:
        instance_name = kwargs.get("instance_name")
        region = kwargs.get("region")
        zone = kwargs.get("zone")
        count = kwargs.get("count", 1)
        bundle_id = kwargs.get("bundle", kwargs.get("bundle_id", "nano_3_0"))
        blueprint_id = kwargs.get("blueprint", kwargs.get("blueprint_id", "debian_12"))
        user_data = kwargs.get("user_data")
        key_pair_name = kwargs.get("key_pair_name")

    region = validate_region(region)
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
            "bundleId": bundle_id,
            "blueprintId": blueprint_id,
        }
        
        if user_data:
            create_kwargs["userData"] = user_data
        if key_pair_name:
            create_kwargs["keyPairName"] = key_pair_name

        if zone:
            candidate_azs = [zone]
        else:
            healthy_azs = get_healthy_azs(client, region)
            candidate_azs = select_az_order(healthy_azs, instance_names[0] if instance_names else instance_name)
            
        last_err = None
        created = False
        for candidate_az in candidate_azs:
            try:
                client.create_instances(availabilityZone=candidate_az, **create_kwargs)
                created = True
                break
            except Exception as e:
                last_err = e
                err_str = str(e).lower()
                if any(k in err_str for k in ["insufficient", "capacity", "availabilityzone", "unavailable", "valid"]):
                    continue
                raise

        if not created and last_err:
            raise last_err
        
    # Fetch all info with smart polling for public IP
    import time
    if instances_to_create:
        initial_wait = min(3 + len(instances_to_create), 8)
        time.sleep(initial_wait)

    results = []
    for name in instance_names:
        ip = ""
        for attempt in range(20):
            try:
                resp = client.get_instance(instanceName=name)
                inst = resp.get("instance", {}) if isinstance(resp, dict) else {}
                ip = inst.get("publicIpAddress", "")
                if ip:
                    break
            except Exception:
                pass
            time.sleep(2)
            
        results.append({
            "name": name,
            "ip": ip
        })
        
    if count == 1:
        return results[0]
    return results


def delete_instance(args=None, **kwargs):
    if args is not None:
        pattern = getattr(args, 'alias', None)
        region = getattr(args, 'region', None)
    else:
        pattern = kwargs.get("alias", kwargs.get("instance_name"))
        region = kwargs.get("region")

    if not pattern:
        raise ValueError("Missing instance name or pattern to delete")

    region = validate_region(region)
    client = boto3.client("lightsail", region_name=region)
    
    # Find all instances matching pattern
    all_inst_resp = client.get_instances()
    instances = all_inst_resp.get("instances", []) if isinstance(all_inst_resp, dict) else []
    
    matched_names = []
    for inst in instances:
        name = inst.get("name", "")
        if name == pattern or fnmatch.fnmatch(name, pattern):
            matched_names.append(name)

    if not matched_names:
        return {"deleted": [], "message": f"No instances found matching '{pattern}' in region {region}"}

    # Query static IPs once to release attached ones
    static_ips_resp = client.get_static_ips()
    static_ips = static_ips_resp.get("staticIps", []) if isinstance(static_ips_resp, dict) else []
    
    attached_static_ips = {}
    for sip in static_ips:
        attached_to = sip.get("attachedTo")
        if attached_to:
            attached_static_ips[attached_to] = sip.get("name")

    deleted_summary = []
    for name in matched_names:
        # Release attached static IP if exists
        released_sip = None
        if name in attached_static_ips:
            sip_name = attached_static_ips[name]
            try:
                client.release_static_ip(staticIpName=sip_name)
                released_sip = sip_name
            except Exception:
                pass

        # Delete instance
        client.delete_instance(instanceName=name)
        deleted_summary.append({
            "name": name,
            "released_static_ip": released_sip
        })

    return {"deleted": deleted_summary, "count": len(deleted_summary)}


def rotate_ip(args=None, **kwargs):
    if args is not None:
        instance_name = getattr(args, 'alias', None)
        region = getattr(args, 'region', None)
        current_ip = getattr(args, 'current_ip', None)
    else:
        instance_name = kwargs.get("instance_name")
        region = kwargs.get("region")
        current_ip = kwargs.get("current_ip")
        
    region = validate_region(region)
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
    create_parser.add_argument("--zone", default=None)
    create_parser.add_argument("--count", type=int, default=1)
    create_parser.add_argument("--bundle", "--bundle-id", dest="bundle", default="nano_3_0")
    create_parser.add_argument("--blueprint", "--blueprint-id", dest="blueprint", default="debian_12")
    create_parser.add_argument("--key", "--key-pair", "--key-pair-name", dest="key_pair_name", default=None)
    
    delete_parser = subparsers.add_parser("delete")
    delete_parser.add_argument("--alias", required=True)
    delete_parser.add_argument("--region", required=True)
    
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
    elif parsed.command == "delete":
        res = delete_instance(args=parsed)
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

