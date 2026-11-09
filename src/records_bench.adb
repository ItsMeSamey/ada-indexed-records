with Ada.Command_Line;
with Ada.Containers.Vectors;
with Ada.Directories;
with Ada.Exceptions;
with Ada.Execution_Time;
with Ada.Real_Time;
with Ada.Streams;
with Ada.Streams.Stream_IO;
with Ada.Strings;
with Ada.Strings.Fixed;
with Ada.Text_IO;
with Indexed_Records;

procedure Records_Bench is
   use type Indexed_Records.U64;
   use type Ada.Real_Time.Time;
   use type Ada.Execution_Time.CPU_Time;

   subtype U64 is Indexed_Records.U64;
   package Binary_IO renames Ada.Streams.Stream_IO;
   package Text_IO renames Ada.Text_IO;
   package Real_Time renames Ada.Real_Time;
   package CPU_Time renames Ada.Execution_Time;
   package Query_Lists is new Ada.Containers.Vectors (Positive, U64);

   type Query_Metric is record
      Ordinal : U64;
      Wall_Seconds, CPU_Seconds : Long_Float;
      Payload_Bytes, Bytes_Read, Bytes_Decoded : U64;
   end record;
   package Metric_Lists is new Ada.Containers.Vectors (Positive, Query_Metric);

   type Pass_Metric is record
      Queries : Metric_Lists.Vector;
      Wall_Seconds, CPU_Seconds : Long_Float := 0.0;
      Read_Wall_Seconds_Sum, Read_CPU_Seconds_Sum : Long_Float := 0.0;
      Payload_Bytes, Bytes_Read, Bytes_Decoded : U64 := 0;
   end record;
   type Pass_Array is array (Positive range 1 .. 3) of Pass_Metric;

   Query_File, JSON_File : Text_IO.File_Type;
   Payload_File : Binary_IO.File_Type;
   Reader : Indexed_Records.Reader;
   Reader_Open : Boolean := False;
   Query_Ordinals : Query_Lists.Vector;
   Passes : Pass_Array;
   Open_Wall_Seconds, Open_CPU_Seconds : Long_Float := 0.0;
   Open_Bytes_Read, Open_Bytes_Decoded : U64 := 0;
   Record_Count, Archive_Size, Index_Size : U64 := 0;
   Reader_Frame_Count : Natural := 0;

   function Image (Value : U64) return String is
     (Ada.Strings.Fixed.Trim (U64'Image (Value), Ada.Strings.Both));

   function Image (Value : Long_Float) return String is
     (Ada.Strings.Fixed.Trim (Long_Float'Image (Value), Ada.Strings.Both));

   function Seconds (Value : Real_Time.Time_Span) return Long_Float is
     (Long_Float (Real_Time.To_Duration (Value)));

   function Add (Left, Right : U64) return U64 is
   begin
      if Left > U64'Last - Right then
         raise Constraint_Error with "benchmark byte count overflow";
      end if;
      return Left + Right;
   end Add;

   function Counter_Delta (After, Before : U64) return U64 is
   begin
      if After < Before then
         raise Constraint_Error with "benchmark counter moved backwards";
      end if;
      return After - Before;
   end Counter_Delta;

   function Parse_Ordinal (Line : String) return U64 is
      Value : U64 := 0;
      Digit : U64;
   begin
      if Line'Length = 0 or else Line'Length > 20 then
         raise Constraint_Error with "query must be an unsigned decimal ordinal";
      end if;
      for Character_Value of Line loop
         if Character_Value not in '0' .. '9' then
            raise Constraint_Error with "query must contain decimal digits only";
         end if;
         Digit := U64 (Character'Pos (Character_Value) - Character'Pos ('0'));
         if Value > (U64'Last - Digit) / 10 then
            raise Constraint_Error with "query ordinal exceeds uint64";
         end if;
         Value := Value * 10 + Digit;
      end loop;
      return Value;
   end Parse_Ordinal;

   procedure Require_New_Output (Path : String) is
   begin
      if Ada.Directories.Exists (Path) then
         raise Text_IO.Use_Error with "benchmark output already exists";
      end if;
   end Require_New_Output;

   procedure Write_Payload (Payload : Indexed_Records.Bytes) is
      Header : Ada.Streams.Stream_Element_Array (1 .. 8);
      Length : U64 := U64 (Payload'Length);
   begin
      for Position in Header'Range loop
         Header (Position) := Ada.Streams.Stream_Element (Length mod 256);
         Length := Length / 256;
      end loop;
      Binary_IO.Write (Payload_File, Header);
      if Payload'Length > 0 then
         Binary_IO.Write (Payload_File, Payload);
      end if;
   end Write_Payload;

   procedure Put (Value : String) is
   begin
      Text_IO.Put (JSON_File, Value);
   end Put;

   procedure Put_Line (Value : String) is
   begin
      Text_IO.Put_Line (JSON_File, Value);
   end Put_Line;

   procedure Write_Report is
      First_Query : Boolean;
   begin
      Put_Line ("{");
      Put_Line ("  ""schema_version"": 1,");
      Put_Line ("  ""passes_count"": 3,");
      Put_Line ("  ""query_count"": " & Image (U64 (Query_Ordinals.Length)) & ",");
      Put_Line ("  ""record_count"": " & Image (Record_Count) & ",");
      Put_Line ("  ""archive_bytes"": " & Image (Archive_Size) & ",");
      Put_Line ("  ""index_bytes"": " & Image (Index_Size) & ",");
      Put_Line ("  ""reader_frame_count"": " & Image (U64 (Reader_Frame_Count)) & ",");
      Put_Line ("  ""payload_file_suffix"": "".payloads"",");
      Put_Line ("  ""payload_encoding"": ""uint64_le_length_then_payload"",");
      Put_Line ("  ""payload_order"": ""pass_then_query"",");
      Put_Line ("  ""query_timing_scope"": ""Read including returned payload construction"",");
      Put_Line ("  ""pass_timing_scope"": ""Read, payload writes and metric collection"",");
      Put_Line ("  ""open"": {");
      Put_Line ("    ""wall_seconds"": " & Image (Open_Wall_Seconds) & ",");
      Put_Line ("    ""cpu_seconds"": " & Image (Open_CPU_Seconds) & ",");
      Put_Line ("    ""bytes_read"": " & Image (Open_Bytes_Read) & ",");
      Put_Line ("    ""bytes_decoded"": " & Image (Open_Bytes_Decoded));
      Put_Line ("  },");
      Put_Line ("  ""passes"": [");
      for Pass_Number in Passes'Range loop
         declare
            Result : Pass_Metric renames Passes (Pass_Number);
         begin
            if Pass_Number > Passes'First then
               Put_Line (",");
            end if;
            Put_Line ("    {");
            Put_Line ("      ""pass"": " & Image (U64 (Pass_Number)) & ",");
            Put_Line ("      ""wall_seconds"": " & Image (Result.Wall_Seconds) & ",");
            Put_Line ("      ""cpu_seconds"": " & Image (Result.CPU_Seconds) & ",");
            Put_Line ("      ""read_wall_seconds_sum"": " & Image (Result.Read_Wall_Seconds_Sum) & ",");
            Put_Line ("      ""read_cpu_seconds_sum"": " & Image (Result.Read_CPU_Seconds_Sum) & ",");
            Put_Line ("      ""bytes_read"": " & Image (Result.Bytes_Read) & ",");
            Put_Line ("      ""bytes_decoded"": " & Image (Result.Bytes_Decoded) & ",");
            Put_Line ("      ""payload_bytes"": " & Image (Result.Payload_Bytes) & ",");
            Put_Line ("      ""queries"": [");
            First_Query := True;
            for Query of Result.Queries loop
               if not First_Query then
                  Put_Line (",");
               end if;
               First_Query := False;
               Put ("        {""ordinal"": " & Image (Query.Ordinal));
               Put (", ""wall_seconds"": " & Image (Query.Wall_Seconds));
               Put (", ""cpu_seconds"": " & Image (Query.CPU_Seconds));
               Put (", ""payload_bytes"": " & Image (Query.Payload_Bytes));
               Put (", ""bytes_read"": " & Image (Query.Bytes_Read));
               Put (", ""bytes_decoded"": " & Image (Query.Bytes_Decoded) & "}");
            end loop;
            Put_Line ("");
            Put_Line ("      ]");
            Put ("    }");
         end;
      end loop;
      Put_Line ("");
      Put_Line ("  ]");
      Put_Line ("}");
   end Write_Report;

   procedure Report_Cleanup_Error
     (Context : String; Failure : Ada.Exceptions.Exception_Occurrence)
   is
   begin
      Text_IO.Put_Line
        (Text_IO.Standard_Error, Context & ": " &
         Ada.Exceptions.Exception_Name (Failure) & ": " &
         Ada.Exceptions.Exception_Message (Failure));
   end Report_Cleanup_Error;

   procedure Cleanup is
   begin
      begin
         if Text_IO.Is_Open (Query_File) then
            Text_IO.Close (Query_File);
         end if;
      exception
         when Failure : others => Report_Cleanup_Error ("query cleanup", Failure);
      end;
      begin
         if Binary_IO.Is_Open (Payload_File) then
            Binary_IO.Close (Payload_File);
         end if;
      exception
         when Failure : others => Report_Cleanup_Error ("payload cleanup", Failure);
      end;
      begin
         if Text_IO.Is_Open (JSON_File) then
            Text_IO.Close (JSON_File);
         end if;
      exception
         when Failure : others => Report_Cleanup_Error ("report cleanup", Failure);
      end;
      begin
         if Reader_Open then
            Indexed_Records.Close (Reader);
            Reader_Open := False;
         end if;
      exception
         when Failure : others => Report_Cleanup_Error ("reader cleanup", Failure);
      end;
   end Cleanup;
begin
   if Ada.Command_Line.Argument_Count /= 3 then
      Text_IO.Put_Line
        (Text_IO.Standard_Error, "usage: records_bench ARCHIVE QUERY_FILE OUTPUT_JSON");
      Ada.Command_Line.Set_Exit_Status (Ada.Command_Line.Failure);
      return;
   end if;

   declare
      Archive_Path : constant String := Ada.Command_Line.Argument (1);
      Query_Path : constant String := Ada.Command_Line.Argument (2);
      Report_Path : constant String := Ada.Command_Line.Argument (3);
      Payload_Path : constant String := Report_Path & ".payloads";
   begin
      Require_New_Output (Report_Path);
      Require_New_Output (Payload_Path);
      Text_IO.Open (Query_File, Text_IO.In_File, Query_Path);
      while not Text_IO.End_Of_File (Query_File) loop
         Query_Ordinals.Append (Parse_Ordinal (Text_IO.Get_Line (Query_File)));
      end loop;
      Text_IO.Close (Query_File);

      declare
         Wall_Start : constant Real_Time.Time := Real_Time.Clock;
         CPU_Start : constant CPU_Time.CPU_Time := CPU_Time.Clock;
         Wall_Stop : Real_Time.Time;
         CPU_Stop : CPU_Time.CPU_Time;
      begin
         Indexed_Records.Open (Reader, Archive_Path);
         Reader_Open := True;
         CPU_Stop := CPU_Time.Clock;
         Wall_Stop := Real_Time.Clock;
         Open_Wall_Seconds := Seconds (Wall_Stop - Wall_Start);
         Open_CPU_Seconds := Seconds (CPU_Stop - CPU_Start);
      end;
      Open_Bytes_Read := Indexed_Records.Bytes_Read (Reader);
      Open_Bytes_Decoded := Indexed_Records.Bytes_Decoded (Reader);
      Record_Count := Indexed_Records.Count (Reader);
      Archive_Size := Indexed_Records.Archive_Bytes (Reader);
      Index_Size := Indexed_Records.Index_Bytes (Reader);
      Reader_Frame_Count := Indexed_Records.Frame_Count (Reader);

      Require_New_Output (Payload_Path);
      Binary_IO.Create (Payload_File, Binary_IO.Out_File, Payload_Path);
      for Pass_Number in Passes'Range loop
         declare
            Result : Pass_Metric renames Passes (Pass_Number);
            Read_Before : constant U64 := Indexed_Records.Bytes_Read (Reader);
            Decoded_Before : constant U64 := Indexed_Records.Bytes_Decoded (Reader);
            Wall_Start : constant Real_Time.Time := Real_Time.Clock;
            CPU_Start : constant CPU_Time.CPU_Time := CPU_Time.Clock;
            Wall_Stop : Real_Time.Time;
            CPU_Stop : CPU_Time.CPU_Time;
         begin
            for Ordinal of Query_Ordinals loop
               declare
                  Query_Read_Before : constant U64 := Indexed_Records.Bytes_Read (Reader);
                  Query_Decoded_Before : constant U64 := Indexed_Records.Bytes_Decoded (Reader);
                  Query_Wall_Start : constant Real_Time.Time := Real_Time.Clock;
                  Query_CPU_Start : constant CPU_Time.CPU_Time := CPU_Time.Clock;
                  Query_Wall_Stop : Real_Time.Time;
                  Query_CPU_Stop : CPU_Time.CPU_Time;
               begin
                  declare
                     Payload : constant Indexed_Records.Bytes :=
                       Indexed_Records.Read (Reader, Ordinal);
                  begin
                     Query_CPU_Stop := CPU_Time.Clock;
                     Query_Wall_Stop := Real_Time.Clock;
                     declare
                        Metric : constant Query_Metric :=
                          (Ordinal => Ordinal,
                           Wall_Seconds => Seconds (Query_Wall_Stop - Query_Wall_Start),
                           CPU_Seconds => Seconds (Query_CPU_Stop - Query_CPU_Start),
                           Payload_Bytes => U64 (Payload'Length),
                           Bytes_Read => Counter_Delta
                             (Indexed_Records.Bytes_Read (Reader), Query_Read_Before),
                           Bytes_Decoded => Counter_Delta
                             (Indexed_Records.Bytes_Decoded (Reader), Query_Decoded_Before));
                     begin
                        Write_Payload (Payload);
                        Result.Queries.Append (Metric);
                        Result.Read_Wall_Seconds_Sum :=
                          Result.Read_Wall_Seconds_Sum + Metric.Wall_Seconds;
                        Result.Read_CPU_Seconds_Sum :=
                          Result.Read_CPU_Seconds_Sum + Metric.CPU_Seconds;
                        Result.Payload_Bytes := Add (Result.Payload_Bytes, Metric.Payload_Bytes);
                     end;
                  end;
               end;
            end loop;
            CPU_Stop := CPU_Time.Clock;
            Wall_Stop := Real_Time.Clock;
            Result.Wall_Seconds := Seconds (Wall_Stop - Wall_Start);
            Result.CPU_Seconds := Seconds (CPU_Stop - CPU_Start);
            Result.Bytes_Read := Counter_Delta (Indexed_Records.Bytes_Read (Reader), Read_Before);
            Result.Bytes_Decoded := Counter_Delta (Indexed_Records.Bytes_Decoded (Reader), Decoded_Before);
         end;
      end loop;
      Binary_IO.Close (Payload_File);
      Indexed_Records.Close (Reader);
      Reader_Open := False;

      Require_New_Output (Report_Path);
      Text_IO.Create (JSON_File, Text_IO.Out_File, Report_Path);
      Write_Report;
      Text_IO.Close (JSON_File);
   end;
   Ada.Command_Line.Set_Exit_Status (Ada.Command_Line.Success);
exception
   when Failure : others =>
      Report_Cleanup_Error ("records_bench", Failure);
      Cleanup;
      Ada.Command_Line.Set_Exit_Status (Ada.Command_Line.Failure);
end Records_Bench;
