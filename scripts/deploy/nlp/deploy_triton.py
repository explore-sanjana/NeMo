# Copyright (c) 2023, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path

from nemo.deploy import DeployPyTriton

LOGGER = logging.getLogger("NeMo")

megatron_llm_supported = True
try:
    from nemo.deploy.nlp import MegatronLLMDeployable
except Exception as e:
    LOGGER.warning(f"Cannot import MegatronLLMDeployable, it will not be available. {type(e).__name__}: {e}")
    megatron_llm_supported = False

trt_llm_supported = True
try:
    from nemo.export.tensorrt_llm import TensorRTLLM
except Exception as e:
    LOGGER.warning(f"Cannot import the TensorRTLLM exporter, it will not be available. {type(e).__name__}: {e}")
    trt_llm_supported = False

vllm_supported = True
try:
    from nemo.export.vllm_exporter import vLLMExporter
except Exception as e:
    LOGGER.warning(f"Cannot import the vLLM exporter, it will not be available. {type(e).__name__}: {e}")
    vllm_supported = False


def get_args(argv):
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description=f"Deploy nemo models to Triton",
    )
    parser.add_argument("-nc", "--nemo_checkpoint", type=str, help="Source .nemo file")
    parser.add_argument(
        "-ptnc",
        "--ptuning_nemo_checkpoint",
        nargs='+',
        type=str,
        required=False,
        help="Source .nemo file for prompt embeddings table",
    )
    parser.add_argument(
        '-ti', '--task_ids', nargs='+', type=str, required=False, help='Unique task names for the prompt embedding.'
    )
    parser.add_argument(
        "-mt",
        "--model_type",
        type=str,
        required=False,
        choices=["gptnext", "gpt", "llama", "falcon", "starcoder", "mixtral", "gemma"],
        help="Type of the model. gptnext, gpt, llama, falcon, and starcoder are only supported."
        " gptnext and gpt are the same and keeping it for backward compatibility",
    )
    parser.add_argument("-tmn", "--triton_model_name", required=True, type=str, help="Name for the service")
    parser.add_argument("-tmv", "--triton_model_version", default=1, type=int, help="Version for the service")
    parser.add_argument(
        "-trp", "--triton_port", default=8000, type=int, help="Port for the Triton server to listen for requests"
    )
    parser.add_argument(
        "-tha", "--triton_http_address", default="0.0.0.0", type=str, help="HTTP address for the Triton server"
    )
    parser.add_argument(
        "-tmr", "--triton_model_repository", default=None, type=str, help="Folder for the trt-llm conversion"
    )
    parser.add_argument("-ng", "--num_gpus", default=1, type=int, help="Number of GPUs for the deployment")
    parser.add_argument("-tps", "--tensor_parallelism_size", default=1, type=int, help="Tensor parallelism size")
    parser.add_argument("-pps", "--pipeline_parallelism_size", default=1, type=int, help="Pipeline parallelism size")
    parser.add_argument(
        "-dt",
        "--dtype",
        choices=["bfloat16", "float16", "fp8", "int8"],
        default="bfloat16",
        type=str,
        help="dtype of the model on TensorRT-LLM or vLLM",
    )
    parser.add_argument("-mil", "--max_input_len", default=256, type=int, help="Max input length of the model")
    parser.add_argument("-mol", "--max_output_len", default=256, type=int, help="Max output length of the model")
    parser.add_argument("-mbs", "--max_batch_size", default=8, type=int, help="Max batch size of the model")
    parser.add_argument("-mnt", "--max_num_tokens", default=None, type=int, help="Max number of tokens")
    parser.add_argument("-ont", "--opt_num_tokens", default=None, type=int, help="Optimum number of tokens")
    parser.add_argument(
        "-mpet", "--max_prompt_embedding_table_size", default=None, type=int, help="Max prompt embedding table size"
    )
    parser.add_argument(
        "-npkc", "--no_paged_kv_cache", default=False, action='store_true', help="Enable paged kv cache."
    )
    parser.add_argument(
        "-drip",
        "--disable_remove_input_padding",
        default=False,
        action='store_true',
        help="Disables the remove input padding option.",
    )
    parser.add_argument(
        "-upe",
        "--use_parallel_embedding",
        default=False,
        action='store_true',
        help='Use parallel embedding feature of TensorRT-LLM.',
    )
    parser.add_argument(
        "-mbm",
        '--multi_block_mode',
        default=False,
        action='store_true',
        help='Split long kv sequence into multiple blocks (applied to generation MHA kernels). \
                        It is beneifical when batchxnum_heads cannot fully utilize GPU.',
    )
    parser.add_argument(
        "-es", '--enable_streaming', default=False, action='store_true', help="Enables streaming sentences."
    )
    parser.add_argument(
        '--use_lora_plugin',
        nargs='?',
        const=None,
        choices=['float16', 'float32', 'bfloat16'],
        help="Activates the lora plugin which enables embedding sharing.",
    )
    parser.add_argument(
        '--lora_target_modules',
        nargs='+',
        default=None,
        choices=[
            "attn_qkv",
            "attn_q",
            "attn_k",
            "attn_v",
            "attn_dense",
            "mlp_h_to_4h",
            "mlp_gate",
            "mlp_4h_to_h",
        ],
        help="Add lora in which modules. Only be activated when use_lora_plugin is enabled.",
    )
    parser.add_argument(
        '--max_lora_rank',
        type=int,
        default=64,
        help='maximum lora rank for different lora modules. '
        'It is used to compute the workspace size of lora plugin.',
    )
    parser.add_argument(
        "-lc", "--lora_ckpt", default=None, type=str, nargs="+", help="The checkpoint list of LoRA weights"
    )
    parser.add_argument(
        "-ucr",
        '--use_cpp_runtime',
        default=False,
        action='store_true',
        help='Use TensorRT LLM C++ runtime',
    )
    parser.add_argument(
        "-b",
        '--backend',
        nargs='?',
        const=None,
        default='TensorRT-LLM',
        choices=['TensorRT-LLM', 'vLLM', 'In-Framework'],
        help="Different options to deploy nemo model.",
    )
    parser.add_argument("-dm", "--debug_mode", default=False, action='store_true', help="Enable debug mode")
    parser.add_argument(
        '-ws',
        '--weight_storage',
        default='auto',
        choices=['auto', 'cache', 'file', 'memory'],
        help='Strategy for storing converted weights for vLLM: "file" - always write weights into a file, '
        '"memory" - always do an in-memory conversion, "cache" - reuse existing files if they are '
        'newer than the nemo checkpoint, "auto" - use "cache" for multi-GPU runs and "memory" '
        'for single-GPU runs.',
    )
    parser.add_argument(
        "-gmu",
        '--gpu_memory_utilization',
        default=0.9,
        type=float,
        help="GPU memory utilization percentage for vLLM.",
    )
    args = parser.parse_args(argv)
    return args


def get_trtllm_deployable(args):
    if args.triton_model_repository is None:
        trt_llm_path = "/tmp/trt_llm_model_dir/"
        LOGGER.info(
            "/tmp/trt_llm_model_dir/ path will be used as the TensorRT LLM folder. "
            "Please set the --triton_model_repository parameter if you'd like to use a path that already "
            "includes the TensorRT LLM model files."
        )
        Path(trt_llm_path).mkdir(parents=True, exist_ok=True)
    else:
        trt_llm_path = args.triton_model_repository

    if args.nemo_checkpoint is None and args.triton_model_repository is None:
        raise ValueError(
            "The provided model repository is not a valid TensorRT-LLM model "
            "directory. Please provide a --nemo_checkpoint."
        )

    if args.nemo_checkpoint is None and not os.path.isdir(args.triton_model_repository):
        raise ValueError(
            "The provided model repository is not a valid TensorRT-LLM model "
            "directory. Please provide a --nemo_checkpoint."
        )

    if args.nemo_checkpoint is not None and args.model_type is None:
        raise ValueError("Model type is required to be defined if a nemo checkpoint is provided.")

    ptuning_tables_files = []
    if not args.ptuning_nemo_checkpoint is None:
        if args.max_prompt_embedding_table_size is None:
            raise ValueError("max_prompt_embedding_table_size parameter is needed for the prompt tuning table(s).")

        for pt_checkpoint in args.ptuning_nemo_checkpoint:
            ptuning_nemo_checkpoint_path = Path(pt_checkpoint)
            if ptuning_nemo_checkpoint_path.exists():
                if ptuning_nemo_checkpoint_path.is_file():
                    ptuning_tables_files.append(pt_checkpoint)
                else:
                    raise IsADirectoryError("Could not read the prompt tuning tables from {0}".format(pt_checkpoint))
            else:
                raise FileNotFoundError("File or directory {0} does not exist.".format(pt_checkpoint))

        if args.task_ids is not None:
            if len(ptuning_tables_files) != len(args.task_ids):
                raise RuntimeError(
                    "Number of task ids and prompt embedding tables have to match. "
                    "There are {0} tables and {1} task ids.".format(len(ptuning_tables_files), len(args.task_ids))
                )

    trt_llm_exporter = TensorRTLLM(
        model_dir=trt_llm_path,
        lora_ckpt_list=args.lora_ckpt,
        load_model=(args.nemo_checkpoint is None),
        use_python_runtime=(not args.use_cpp_runtime),
    )

    if args.nemo_checkpoint is not None:
        try:
            LOGGER.info("Export operation will be started to export the nemo checkpoint to TensorRT-LLM.")
            trt_llm_exporter.export(
                nemo_checkpoint_path=args.nemo_checkpoint,
                model_type=args.model_type,
                n_gpus=args.num_gpus,
                tensor_parallelism_size=args.tensor_parallelism_size,
                pipeline_parallelism_size=args.pipeline_parallelism_size,
                max_input_len=args.max_input_len,
                max_output_len=args.max_output_len,
                max_batch_size=args.max_batch_size,
                max_num_tokens=args.max_num_tokens,
                opt_num_tokens=args.opt_num_tokens,
                use_parallel_embedding=args.use_parallel_embedding,
                max_prompt_embedding_table_size=args.max_prompt_embedding_table_size,
                paged_kv_cache=(not args.no_paged_kv_cache),
                remove_input_padding=(not args.disable_remove_input_padding),
                dtype=args.dtype,
                enable_multi_block_mode=args.multi_block_mode,
                use_lora_plugin=args.use_lora_plugin,
                lora_target_modules=args.lora_target_modules,
                max_lora_rank=args.max_lora_rank,
                save_nemo_model_config=True,
            )
        except Exception as error:
            raise RuntimeError("An error has occurred during the model export. Error message: " + str(error))

    try:
        for i, prompt_embeddings_checkpoint_path in enumerate(ptuning_tables_files):
            if args.task_ids is not None:
                task_id = args.task_ids[i]
            else:
                task_id = i

            LOGGER.info(
                "Adding prompt embedding table: {0} with task id: {1}.".format(
                    prompt_embeddings_checkpoint_path, task_id
                )
            )
            trt_llm_exporter.add_prompt_table(
                task_name=str(task_id),
                prompt_embeddings_checkpoint_path=prompt_embeddings_checkpoint_path,
            )
    except Exception as error:
        raise RuntimeError(
            "An error has occurred during adding the prompt embedding table(s). Error message: " + str(error)
        )
    return trt_llm_exporter


def get_vllm_deployable(args):
    if args.ptuning_nemo_checkpoint is not None:
        raise ValueError("vLLM backend doesn't support P-tuning at this time.")
    if args.lora_ckpt is not None:
        raise ValueError("vLLM backend doesn't support LoRA at this time.")

    tempdir = None
    model_dir = args.triton_model_repository
    if model_dir is None:
        tempdir = tempfile.TemporaryDirectory()
        model_dir = tempdir.name
        LOGGER.info(
            f"{model_dir} path will be used as the vLLM intermediate folder. "
            + "Please set the --triton_model_repository parameter if you'd like to use a path that already "
            + "includes the vLLM model files."
        )
    elif not os.path.exists(model_dir):
        os.makedirs(model_dir)

    try:
        exporter = vLLMExporter()
        exporter.export(
            nemo_checkpoint=args.nemo_checkpoint,
            model_dir=model_dir,
            model_type=args.model_type,
            tensor_parallel_size=args.num_gpus,
            max_model_len=args.max_input_len + args.max_output_len,
            dtype=args.dtype,
            weight_storage=args.weight_storage,
            gpu_memory_utilization=args.gpu_memory_utilization,
        )
        return exporter
    except Exception as error:
        raise RuntimeError("An error has occurred during the model export. Error message: " + str(error))
    finally:
        if tempdir is not None:
            tempdir.cleanup()


def get_nemo_deployable(args):
    if args.nemo_checkpoint is None:
        raise ValueError("In-Framework deployment requires a .nemo checkpoint")

    return MegatronLLMDeployable(args.nemo_checkpoint, args.num_gpus)


def nemo_deploy(argv):
    args = get_args(argv)

    if args.debug_mode:
        loglevel = logging.DEBUG
    else:
        loglevel = logging.INFO

    LOGGER.setLevel(loglevel)
    LOGGER.info("Logging level set to {}".format(loglevel))
    LOGGER.info(args)

    backend = args.backend.lower()
    if backend == 'tensorrt-llm':
        if not trt_llm_supported:
            raise ValueError("TensorRT-LLM engine is not supported in this environment.")
        triton_deployable = get_trtllm_deployable(args)
    elif backend == 'in-framework':
        if not megatron_llm_supported:
            raise ValueError("MegatronLLMDeployable is not supported in this environment.")
        triton_deployable = get_nemo_deployable(args)
    elif backend == 'vllm':
        if not vllm_supported:
            raise ValueError("vLLM engine is not supported in this environment.")
        triton_deployable = get_vllm_deployable(args)
    else:
        raise ValueError("Backend: {0} is not supported.".format(backend))

    try:
        nm = DeployPyTriton(
            model=triton_deployable,
            triton_model_name=args.triton_model_name,
            triton_model_version=args.triton_model_version,
            max_batch_size=args.max_batch_size,
            port=args.triton_port,
            address=args.triton_http_address,
            streaming=args.enable_streaming,
        )

        LOGGER.info("Triton deploy function will be called.")
        nm.deploy()
    except Exception as error:
        LOGGER.error("Error message has occurred during deploy function. Error message: " + str(error))
        return

    try:
        LOGGER.info("Model serving on Triton is will be started.")
        nm.serve()
    except Exception as error:
        LOGGER.error("Error message has occurred during deploy function. Error message: " + str(error))
        return

    LOGGER.info("Model serving will be stopped.")
    nm.stop()


if __name__ == '__main__':
    nemo_deploy(sys.argv[1:])
