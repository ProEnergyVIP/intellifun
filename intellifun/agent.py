from dataclasses import dataclass
import inspect
import json

from intellifun.LLM import get_random_error_message
from intellifun.debug import is_debug
from intellifun.message import (Function, SystemMessage,
                                ToolMessage, ToolMessageGroup, UserMessage, AgentUsage)

from intellifun.message import print_message

@dataclass
class Tool:
    name: str
    func: callable
    description: str
    parameters: dict
    prompt: str = None

    __called_times = 0

    def check_call_limit(self, limit=10):
        '''Check if the tool has been called too many times'''
        if self.__called_times >= limit:
            return False
        return True
    
    def increment_call_count(self):
        '''Increment the call count of the tool'''
        self.__called_times += 1


MAX_RECENT_CALLS = 5  # Only track the last 5 calls

class Agent:
    def __init__(self, llm, tools=None, sys_prompt='', memory=None, context=None, json_reply=False):
        self.llm = llm
        self.tools = tools or []
        self.sys_msg = sys_prompt if isinstance(sys_prompt, SystemMessage) else SystemMessage(content=sys_prompt)
        self.memory = memory
        self.context = context
        self.json_reply = json_reply
        # Track recent tool calls to detect repetition
        self._recent_tool_calls = []

    def _is_repeated_tool_call(self, func: Function) -> bool:
        '''Check if this exact tool call was made recently'''
        current_call = (func.name, func.arguments)
        # Look for the same tool name and arguments in recent calls
        return current_call in self._recent_tool_calls

    def _add_tool_call(self, func: Function):
        '''Add a tool call to the recent calls list'''
        current_call = (func.name, func.arguments)
        self._recent_tool_calls.append(current_call)
        # Keep only the most recent calls
        self._recent_tool_calls = self._recent_tool_calls[-MAX_RECENT_CALLS:]

    def ask(self, message, user_name=None, usage=None):
        '''Ask a question to the agent, and get a response

        Args:
            message (str or Message): The message to ask
            user_name (str, optional): The name of the user. Defaults to None.
            usage (AgentUsage, optional): Object to accumulate token usage across models.
                You can pass an AgentUsage object to track usage across multiple calls.

        Returns:
            str: The response from the agent
        '''
        reply = None
        agent_usage = AgentUsage()  # Track total usage across all calls
        
        # Get history messages from memory
        history_msgs = self.memory.load_memory() if self.memory else []
        
        # Add the user's message to the conversation
        if isinstance(message, str):
            message = UserMessage(content=message, user_name=user_name)
        
        conversation = [message]
        # Main conversation loop
        for _ in range(10):  # Limit to 10 iterations to prevent infinite loops
            print_message(self.sys_msg)

            msgs = [*history_msgs, *conversation]
            for m in msgs:
                print_message(m)
            
            # call the model
            try:
                ai_msg = self.llm.call(self.sys_msg, msgs, tools=self.tools)
                # Add usage to AgentUsage if available
                if ai_msg.usage and ai_msg.model:
                    agent_usage.add_usage(ai_msg.model, ai_msg.usage)
            except Exception as e:
                err_msg = get_random_error_message()
                reply = {'message': err_msg} if self.json_reply else err_msg
                break
            
            print_message(ai_msg)
            
            conversation.append(ai_msg)
            # check if we need to run a tool
            if ai_msg.tool_calls is not None:
                tool_msgs = self.process_func_call(ai_msg)
                conversation.append(tool_msgs)
            elif ai_msg.content:
                try:
                    reply = json.loads(ai_msg.content) if self.json_reply else ai_msg.content
                except json.JSONDecodeError as e:
                    err_msg = f'Error processing JSON message: {e}. Make sure your response is a valid JSON string and do not include the `json` tag.'
                    conversation.append(UserMessage(content=err_msg))
                    continue

                self.memory.add_messages(conversation)
                print(agent_usage.format())
                if usage:
                    usage.merge(agent_usage)
                return reply

        self.memory.add_messages(conversation)
        print(agent_usage.format())
        if usage:
            usage.merge(agent_usage)
        return reply if reply is not None else 'Sorry, I am not sure how to answer that.'


    def process_func_call(self, ai_msg):
        '''Process the function call in the LLM result'''
        msgs = []
        for fc in ai_msg.tool_calls:
            # Check if this is a repeated tool call
            if self._is_repeated_tool_call(fc.function):
                msg = f'Tool "{fc.function.name}" was just called with the same arguments again. To prevent loops, please try a different approach or different arguments.'
                msgs.append(ToolMessage(content=msg, tool_call_id=fc.id))
                continue

            func_res = self.run_tool_func(fc.function)
            tool_res_msg = ToolMessage(content=func_res, tool_call_id=fc.id)
            msgs.append(tool_res_msg)

            # Track this tool call
            self._add_tool_call(fc.function)
        
        msg_group = ToolMessageGroup(tool_messages=msgs)
        
        return msg_group

    def run_tool_func(self, func: Function):
        '''Run the given tool function and return the result'''
        tool_name = func.name
        
        for tool in self.tools:
            if tool.name == tool_name:
                if not tool.check_call_limit():
                    self.tools.remove(tool)
                    return f'Tool "{tool_name}" has been called too many times, it will be removed from the list of available tools.'
                
                try:
                    tool_input = json.loads(func.arguments) if isinstance(func.arguments, str) else func.arguments
                    
                    sig = inspect.signature(tool.func)
                    num_params = len(sig.parameters)
  
                    if num_params == 0:
                        res = tool.func()
                    elif num_params == 1:
                        res = tool.func(tool_input)
                    elif num_params == 2:
                        res = tool.func(tool_input, self.context)
                    elif num_params == 3:
                        res = tool.func(tool_input, self.context, self)
                    else:
                        return f'Invalid number of parameters for tool function {tool_name}: {num_params}'
                    
                    tool.increment_call_count()

                    # check result data type and wrap it into a ToolFuncResult
                    if isinstance(res, dict):
                        msg = res['message'] if 'message' in res else f'tool function {tool_name} finished'
                        return msg
                    if isinstance(res, str):
                        return res

                    return f'tool function {tool_name} finished'
                except json.JSONDecodeError as e:
                    return f'Error decoding JSON parameter for "{tool_name}": {e}. Use valid JSON string without the `json` tag.'
                except Exception as e:
                    if is_debug:
                        import traceback
                        traceback.print_exc()

                    return f'Error running tool "{tool_name}": {e}'
        
        return f'No tool named "{tool_name}" found. Do not call it again.'
