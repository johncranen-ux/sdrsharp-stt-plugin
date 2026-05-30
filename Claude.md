# SDR# Speech-to-Text

## Goal
The goal is to transcribe Voice received in SDR# and to display as Text with the least errors

## Business Requirements

- Speech Chunks received in the SDR# application need to be sent to a STT server by a SDR# Plugin and the transcribed text, sent back to the Plugin where it shall be displayed
- The STT server is running locally in WSL Ubuntu while the SDR# and the Plugin run under Windows 11 (see whisper_gpu_setup_guide.docx ion the project directory as to how it was installed and how to use it)
- The GPU is a AMD RX7900xtx and has 24 Gb VRAM
- The SDR# is developed in .Net and the Plugin should also be developed using .Net 8
- The Plugin should support Voice Activation Detection (VAD), so no empty or Static chunks are sent to the STT Server 
- The channels SDR# is listening to are the Nautical and later the Aviation Band
- Both are using English as the main language, but may contain static and dialects (as they are very international), making it hard to interpret
- The Wisper Server will be using the Large-Turbo model for this purpose 

## Additional Features for next releases

These features shall be implemented once the above requirements have been stably implemented

- Should the transmuting be poor, the text should be analyzed by an local LLM, matching frequently used nautical/aviation terms to make more sense of the text
- an example would be "Maas Approach" instead of "Mass Aproach"
- The LLM for analysing should also run locally on the GPU, so check which open model would be best for this job taking into consideration that it willhave to run next to the whisper model on the GPU
- Vessel names are often misheard and an API To retrieve AIS information of vessels near the Rotterdam Harbour, to narrow the correct name, could be used
- Display additional information about the Vessel

## Technical Details

- The PC is running Windows 11
- The PC uses an AMD rt7900xtx GPU which requires ROCm instead of CUDA
- Development of the SDR# Plugin must be in C# and using .Net 8
- For Reference you can look at a similar project in the WhisperPlugin folder in the project directory
- SDR# ddl's that need to be used can be found in D:\SDR\SdrSharpSDK\sdrplugins\lib
- SDR# itself can be found and started from the D:\SDR\SDRSharp folder
- That folder also contains the Plugins folder where the Plugin dll needs to be stored
- This previous Project was running under Windows 10 and using an NVIDIA GPU with far less VRAM


## Strategy

1. Analyze the whisper_gpu_setup_guide.docx document
2. Check the internet to come up with the best setup for implementation. The reference project may be used, but if you have a better approach, architecture, solution, please use them (maybe the py proxy is a good idea, but maybe you have a better one ...)
3. Ask questions upto 3 iterations if you need further clarification
4. Write plan with success criteria for each phase to be checked off. Include project scaffolding, including .gitignore, and rigorous unit testing (where possible).
5. Execute the plan ensuring all Business requirements are met - feel free to build and deploy the software. If you can run the software yourself for testing, please do so. If you need help, please tell so
7. If you run into issues, first analyse the before fixing and test to see the issue is fixed before continuing
8. Carry out extensive integration testing where possible, fixing defects
9. Only complete when the first release covering the Business Requirements, is finished and tested, with the server running and ready for the user
10. Don't implement the Additional features yet, but take them into consideration while designing the first release, so the chosen desgn/approach/architecture is ready

## Coding standards

1. Use latest versions of libraries and idiomatic approaches as of today (make use of context 7 if required)
2. Check the internet for the SDR SDK and dll's to find out how to use them
2. Do check the internet for dependencies so you don't run into them when it is too late
