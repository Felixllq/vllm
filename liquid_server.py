from fastapi import FastAPI
from server import HttpRequestBody, UvicornServer
import uvicorn
import subprocess
from vllm import LLM, SamplingParams, RequestOutput
from typing import List, Dict, Tuple
import json

model_name = "facebook/opt-6.7b"
class LiquidServer:
    def __init__(self) -> None:
        self.fastapi_app = FastAPI()
        self.llm = LLM(
            model_name, 
            enforce_eager=True,
            # load_format="auto",
            # tensor_parallel_size=2,
            liquid_gpu_range = [0,1],
            liquid_gpu_space = 32,
            liquid_driver_gpu_id = 0, 
            liquid_total_num_shards = 2,
            # gpu_memory_utilization=0.7,
        )
        @self.fastapi_app.post("/v1/completions")
        async def enqueue_request(r: HttpRequestBody) -> None:
            print(f"{r.request_id} received!")
            max_model_length = self.llm.llm_engine.model_config.max_model_len
            sampling_params = SamplingParams(max_tokens=r.max_response_length+1, min_tokens=r.max_response_length, temperature=0)
            self.llm._add_request(
                inputs=r.prompt,
                params=sampling_params
            )
        self.http_server = UvicornServer(
            uvicorn.Config(
                app=self.fastapi_app,
                host="localhost",
                port=8000,
            )
        )

    def start(self):

        self.http_server.start()

        command = [
                './LLMLoadgen',
                '-pattern', 'azure-multiplex-70-5',
                '-dataset', 'azure-multiplex',
                '-dst', 'liquid',
                '-ip', 'localhost',
                '-port', '8000',
                '-limit', '100',
                '-max_drift', '100',
                '-model_name', f'{model_name}'
            ]
        working_dir = './LLMLoadgen/LLMLoadgen-0.9/release'
        loadgen_process = subprocess.Popen(command, cwd=working_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        loadgen_running = True
        request_outputs: List[RequestOutput] = []
        while loadgen_running:
            self.llm._run_engine(use_tqdm=False)
            while self.llm.llm_engine.request_output_queue.qsize() != 0:
                request_output = self.llm.llm_engine.request_output_queue.get()
                print(f"request: {request_output.request_id} finished!")
                request_outputs.append(request_output)

            loadgen_running = (loadgen_process.poll() is None)
        print(f"All requests have been processed!")

        # store all the results
        timestamps = self.llm.llm_engine.auto_scaler.timestamp_records
        tp_level_records = self.llm.llm_engine.auto_scaler.tp_level_records
        cache_usage_records = self.llm.llm_engine.auto_scaler.cache_usage_records
        
        arrival_times = []
        e2e_latencys = []
        queueing_latencys = []
        serving_latencys = []
        for request_output in request_outputs:
            metrics = request_output.metrics
            
            e2e_latency = metrics.finished_time - metrics.arrival_time
            queueing_latency = metrics.time_in_queue if metrics.time_in_queue else 0
            serving_latency = e2e_latency - queueing_latency

            arrival_times.append(metrics.arrival_time)
            e2e_latencys.append(e2e_latency)
            queueing_latencys.append(queueing_latency)
            serving_latencys.append(serving_latency)

        data = {
            "timestamps": timestamps,
            "tp_level_records": tp_level_records,
            "cache_usage_records": cache_usage_records,
            "arrival_times": arrival_times,
            "e2e_latencys": e2e_latencys,
            "queueing_latencys": queueing_latencys,
            "serving_latencys": serving_latencys,
        }

        # Dump the data to a JSON file
        with open('liquid_results/liquid_results.json', 'w') as json_file:
            json.dump(data, json_file, indent=4)  # indent=4 for pretty printing
        
        del self.llm
        self.http_server.stop()


if __name__ == '__main__':
    liquid_server = LiquidServer()
    liquid_server.start()