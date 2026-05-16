from abc import ABC, abstractmethod


class Parser(ABC):
    @abstractmethod
    def parse(self, row: dict) -> dict:
        pass

class DnsParser(Parser):
    
    def parse(self, row: dict) -> dict:
        return {
            "timestamp": row.get("Timestamp", ""), 
            "log_attribute": "network_dns",
            "attributes": {
                "source_ip": row.get("SourceIP", ""),
                "destination_ip": row.get("DestinationIP", ""),
                "dns_query": row.get("DnsQuery", ""),
                "dns_response_code": row.get("DnsResponseCode", None)
            },
            "labels": {                                            
                "sus": row.get("sus", 0),
                "evil": row.get("evil", 0)
            }
        }

class DeepKernelParser(Parser):
    def parse(self, row:dict) -> dict:
        return {
            "timestamp": str(row.get("timestamp", "")),
            "event_name": row.get("eventName", "unknown_kernel_event"),
            "attributes": {
                # Inherited Host Features
                "host_name": row.get("hostName", ""),
                "event_id": row.get("eventId", ""),
                "return_value": row.get("returnValue", ""),
                "process_id": row.get("processId", ""),
                "parent_process_id": row.get("parentProcessId", ""),
                "process_name": row.get("processName", ""),
                "user_id": row.get("userId", ""),
                
                # Deep Kernel Specific Features
                "thread_id": row.get("threadId", ""),
                "mount_namespace": row.get("mountNamespace", ""),
                "stack_addresses": row.get("stackAddresses", "")
            },
            "labels": {
                "sus": row.get("sus", 0),
                "evil": row.get("evil", 0)
            }
        }

class StandardHostParser(Parser):
    def parse(self, row: dict) -> dict:
        return {
            "timestamp": str(row.get("timestamp", "")),
            "event_name": row.get("eventName", "unknown_event"), 
            "attributes": {
                "host_name": row.get("hostName", ""),            
                "event_id": row.get("eventId", ""),              
                "return_value": row.get("returnValue", ""),     
                "process_id": row.get("processId", ""),
                "parent_process_id": row.get("parentProcessId", ""),
                "process_name": row.get("processName", ""),
                "user_id": row.get("userId", "")
            },
            "labels": {
                "sus": row.get("sus", 0),
                "evil": row.get("evil", 0)
            }
        }
        
