
import asyncio
import json
import os
import queue
import threading
import time
import requests
import pyaudio
import websockets
from dotenv import load_dotenv
from deepgram.audio.microphone import Microphone
from deepgram import DeepgramClient, DeepgramClientOptions, LiveTranscriptionEvents, LiveOptions
from langchain_core.prompts import ChatPromptTemplate
from langchain_groq import ChatGroq
from langchain.memory import ConversationBufferMemory
from langchain.prompts import (
    MessagesPlaceholder,
    SystemMessagePromptTemplate,
    HumanMessagePromptTemplate,
)
from langchain.chains import LLMChain

load_dotenv()

ELEVEN_LABS_API_KEY = os.getenv("ELEVEN_LABS_API_KEY")
DEFAULT_URL = f"wss://api.deepgram.com/v1/speak?encoding=linear16&sample_rate=48000"
DEFAULT_TOKEN = os.getenv("DEEPGRAM_API_KEY")

TIMEOUT = 0.050
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 48000
CHUNK = 8000

class Speaker:
    def __init__(self, rate=RATE, chunk=CHUNK, channels=CHANNELS):
        self._exit = threading.Event()
        self._queue = queue.Queue()
        self._audio = pyaudio.PyAudio()
        self._stream = None
        self._thread = None
        self._chunk = chunk
        self._rate = rate
        self._format = FORMAT
        self._channels = channels

    def start(self):
        self._stream = self._audio.open(
            format=self._format,
            channels=self._channels,
            rate=self._rate,
            input=False,
            output=True,
            frames_per_buffer=self._chunk,
        )
        self._exit.clear()
        self._thread = threading.Thread(target=self._play, daemon=True)
        self._thread.start()

    def stop(self):
        self._exit.set()
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        self._thread.join()
        self._thread = None

    def play(self, data):
        self._queue.put(data)

    def _play(self):
        while not self._exit.is_set():
            try:
                data = self._queue.get(True, TIMEOUT)
                self._stream.write(data)
            except queue.Empty:
                pass

class LanguageModelProcessor:
    def __init__(self):
        self.llm = ChatGroq(temperature=0, model_name="mixtral-8x7b-32768", groq_api_key=os.getenv("GROQ_API_KEY"))
        self.memory = ConversationBufferMemory(memory_key="chat_history", return_messages=True)

        with open('system_prompt.txt', 'r') as file:
            system_prompt = file.read().strip()
        
        self.prompt = ChatPromptTemplate.from_messages([
            SystemMessagePromptTemplate.from_template(system_prompt),
            MessagesPlaceholder(variable_name="chat_history"),
            HumanMessagePromptTemplate.from_template("{text}")
        ])

        self.conversation = LLMChain(
            llm=self.llm,
            prompt=self.prompt,
            memory=self.memory
        )

    def process(self, text):
        self.memory.chat_memory.add_user_message(text)

        start_time = time.time()
        response = self.conversation.invoke({"text": text})
        end_time = time.time()

        self.memory.chat_memory.add_ai_message(response['text'])
        elapsed_time = int((end_time - start_time) * 1000)
        print(f"LLM ({elapsed_time}ms): {response['text']}")
        return response['text']

class TextToSpeech:
    def __init__(self):
        self.speaker = Speaker()
        self.speaker.start()
        self.voice_model = 'aura-helios-en'  # British male voice model

    async def stream_text(self, text):
        url = f"wss://api.deepgram.com/v1/speak?encoding=linear16&sample_rate=48000&model={self.voice_model}"
        async with websockets.connect(
            url,
            extra_headers={"Authorization": f"Token {DEFAULT_TOKEN}"}
        ) as websocket:
            await websocket.send(json.dumps({"type": "Speak", "text": text}))
            await websocket.send(json.dumps({"type": "Flush"}))
            while True:
                message = await websocket.recv()
                if isinstance(message, bytes):
                    self.speaker.play(message)

    def stop(self):
        self.speaker.stop()


class TranscriptCollector:
    def __init__(self):
        self.reset()

    def reset(self):
        self.transcript_parts = []

    def add_part(self, part):
        self.transcript_parts.append(part)

    def get_full_transcript(self):
        return ' '.join(self.transcript_parts)

transcript_collector = TranscriptCollector()

async def get_transcript(callback):
    transcription_complete = asyncio.Event()
    timeout_duration = 10
    try:
        config = DeepgramClientOptions(options={"keepalive": "true"})
        deepgram: DeepgramClient = DeepgramClient("", config)

        # Use asyncwebsocket instead of asynclive
        dg_connection = deepgram.listen.asyncwebsocket.v("1")
        print("Listening...")

        # Modify the on_message handler to accept variable arguments
        async def on_message(*args, **kwargs):
            result = kwargs.get("result")  # Access 'result' safely from kwargs
            if not result:
                return  # Exit if 'result' is not found
            
            sentence = result.channel.alternatives[0].transcript
            
            if not result.speech_final:
                transcript_collector.add_part(sentence)
            else:
                transcript_collector.add_part(sentence)
                full_sentence = transcript_collector.get_full_transcript()
                if len(full_sentence.strip()) > 0:
                    full_sentence = full_sentence.strip()
                    print(f"Human: {full_sentence}")
                    
                    # Await the async callback
                    await callback(full_sentence)
                    
                    transcript_collector.reset()
                    transcription_complete.set()

        # Register the modified on_message callback
        dg_connection.on(LiveTranscriptionEvents.Transcript, on_message)

        async def timeout_task():
            await asyncio.sleep(timeout_duration)
            if not transcription_complete.is_set():
                print("No input detected, disconnecting...")
                transcription_complete.set()
                
        asyncio.create_task(timeout_task())

        options = LiveOptions(
            model="nova-2",
            punctuate=True,
            language="en-US",
            encoding="linear16",
            channels=1,
            sample_rate=16000,
            endpointing=300,
            smart_format=True,
        )

        await dg_connection.start(options)
        microphone = Microphone(dg_connection.send)
        microphone.start()

        await transcription_complete.wait()
        microphone.finish()
        await dg_connection.finish()

    except Exception as e:
        print(f"Could not open socket: {e}")

class ConversationManager:
    def __init__(self):
        self.transcription_response = ""
        self.llm = LanguageModelProcessor()
        self.tts = TextToSpeech()

    async def main(self):
        async def handle_full_sentence(full_sentence):
            # Process user input with LLM
            llm_response = self.llm.process(full_sentence)
        
            
            # Send LLM response to TTS for playback, ensuring it is awaited
            await self.tts.stream_text(llm_response)

        # Main loop to keep listening and processing
        try:
            while True:
                await get_transcript(handle_full_sentence)
                
                # Check for stop command (like "goodbye")
                if "goodbye" in self.transcription_response.lower():
                    break
        except KeyboardInterrupt:
            print("Keyboard interrupt detected. Shutting down gracefully.")
        finally:
            # Stop TTS when loop ends
            self.tts.stop()
            await self.close_tasks()

    async def close_tasks(self):
        # Cancel all remaining tasks to clean up
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        [task.cancel() for task in tasks]
        await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == "__main__":
    try:
        asyncio.run(ConversationManager().main())
    except KeyboardInterrupt:
        print("Exiting program.")
